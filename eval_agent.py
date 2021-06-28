"""
Evaluate a local agent on a remote server.
"""

from argparse import ArgumentParser, Namespace
from json import JSONDecodeError
from pathlib import Path
import socket
import sys
from typing import Any, List, Optional, Tuple
import zlib

import ray
from ray.rllib.utils.typing import TensorType

from hearts_gym import utils
from hearts_gym.envs.hearts_env import Reward
from hearts_gym.server import utils as server_utils
from hearts_gym.server.hearts_server import (
    Client,
    HeartsRequestHandler,
    HeartsServer,
    SERVER_ADDRESS,
    PORT,
)
from hearts_gym.policies import RandomPolicy, RuleBasedPolicy

SERVER_TIMEOUT_SEC = HeartsServer.PRINT_INTERVAL_SEC + 5
ENV_NAME = 'Hearts-v0'
LEARNED_POLICY_ID = 'learned'


def parse_args() -> Namespace:
    """Parse command line arguments for evaluating an agent against
    a server.

    Returns:
        Namespace: Parsed arguments.
    """
    parser = ArgumentParser()

    parser.add_argument(
        'checkpoint_path',
        type=str,
        help='Path of model checkpoint to load for evaluation.',
    )
    parser.add_argument(
        '--name',
        type=str,
        help='Name to register',
    )
    parser.add_argument(
        '--algorithm',
        type=str,
        default='PPO',
        help='Model algorithm to use.',
    )
    parser.add_argument(
        '--framework',
        type=str,
        default=utils.DEFAULT_FRAMEWORK,
        help='Framework used for training.',
    )

    parser.add_argument(
        '--server_address',
        type=str,
        default=SERVER_ADDRESS,
        help='Server address to connect to.',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=PORT,
        help='Server port to connect to.',
    )

    return parser.parse_args()


def _is_done(num_games: int, max_num_games: Optional[int]) -> bool:
    """Return whether the desired number of games have been played..

    Returns:
        bool: Whether the desired number of games have been played.
    """
    return HeartsRequestHandler.is_done(num_games, max_num_games)


def _receive_data_shard(
        client: socket.socket,
        max_receive_bytes: int,
) -> bytes:
    """Return a message received from the server in a failsafe way.

    If the server stopped, exit the program.

    Args:
        client (socket.socket): Socket of the client.
        max_receive_bytes (int): Number of bytes to receive at maximum.

    Returns:
        Any: Message data received.
    """
    try:
        data = client.recv(max_receive_bytes)
    except Exception:
        print('Unable to receive data from server.')
        raise

    if data == b'' or data is None:
        print('Server stopped. Exiting...')
        sys.exit(0)

    return data


def _receive_msg_length(
        client: socket.socket,
        max_receive_bytes: int,
) -> Tuple[int, bytes]:
    """Return the expected length of a message received from the server
    in a failsafe way.

    To be more efficient, receive more data than necessary. Any
    additional data is returned.

    If the server stopped, exit the program.

    Args:
        client (socket.socket): Socket of the client.
        max_receive_bytes (int): Number of bytes to receive at maximum
            per message shard.

    Returns:
        int: Amount of bytes in the rest of the message.
        bytes: Extraneous part of message data received.
    """
    data_shard = _receive_data_shard(client, max_receive_bytes)
    total_num_received_bytes = len(data_shard)
    data = [data_shard]
    length_end = data_shard.find(server_utils.MSG_LENGTH_SEPARATOR)
    while (
            length_end == -1
            and total_num_received_bytes < server_utils.MAX_MSG_PREFIX_LENGTH
    ):
        data_shard = _receive_data_shard(client, max_receive_bytes)
        total_num_received_bytes += len(data_shard)
        data.append(data_shard)
        length_end = data_shard.find(server_utils.MSG_LENGTH_SEPARATOR)

    assert length_end != -1, 'server did not send message length'

    length_end += total_num_received_bytes - len(data_shard)
    data = b''.join(data)
    msg_length = int(data[:length_end])
    extra_data = data[length_end + len(server_utils.MSG_LENGTH_SEPARATOR):]

    return msg_length, extra_data


def receive_data(
        client: socket.socket,
        max_receive_bytes: int,
        max_total_receive_bytes: int,
) -> Any:
    """Return data received from the server in a failsafe way.

    If the server stopped, exit the program. If the message could not be
    decoded, return an error message string.

    Args:
        client (socket.socket): Socket of the client.
        max_receive_bytes (int): Number of bytes to receive at maximum
            per message shard.
        max_total_receive_bytes (int): Number of bytes to receive at
            maximum, that is, summed over all message shards.

    Returns:
        Any: Data received or an error message string if there
            were problems.
    """
    msg_length, data_shard = _receive_msg_length(client, max_receive_bytes)
    assert msg_length < max_total_receive_bytes, 'message is too long'

    total_num_received_bytes = len(data_shard)
    data = [data_shard]
    while total_num_received_bytes < msg_length:
        data_shard = _receive_data_shard(client, max_receive_bytes)
        total_num_received_bytes += len(data_shard)
        data.append(data_shard)

    assert total_num_received_bytes == msg_length, \
        'message does not match length'

    data = b''.join(data)
    try:
        data = server_utils.decode_data(data)
    except (JSONDecodeError, zlib.error) as ex:
        print('Failed decoding:', data)
        print('Error message:', str(ex))
        return '[See decoding error message.]'
    return data


def wait_for_data(
        client: socket.socket,
        max_receive_bytes: int,
        max_total_receive_bytes: int,
) -> Any:
    """Continually receive data from the server the given client is
    connected to.

    Whenever the data received is a string, print it and receive
    data again.

    Args:
        client (socket.socket): Socket of the client.
        max_receive_bytes (int): Number of bytes to receive at maximum
            per message shard.
        max_total_receive_bytes (int): Number of bytes to receive at
            maximum per single message, that is, summed over all
            message shards of a single message.

    Returns:
        Any: Non-string data received.
    """
    data = receive_data(client, max_receive_bytes, max_total_receive_bytes)
    while isinstance(data, str):
        server_utils.send_ok(client)
        print('Server says:', data)
        data = receive_data(client, max_receive_bytes, max_total_receive_bytes)
    return data


def _take_indices(data: List[Any], indices: List[int]) -> List[Any]:
    """Return the elements obtained by indexing into the given data
    according to the given indices.

    Args:
        data (List[Any]): List to multi-index.
        indices (List[int]): Indices to use; are used in the
            order they are given in.

    Returns:
        List[Any]: Elements obtained by multi-indexing into the
            given data.
    """
    return [data[i] for i in indices]


def _update_indices(
        values: List[Any],
        indices: List[int],
        new_values: List[Any],
) -> None:
    """Update the given list of values with new elements according to
    the given indices.

    Args:
        values (List[Any]): List to multi-update.
        indices (List[int]): Indices to use; are used in the
            order they are given in.
        new_values (List[Any]): Updated values; one for each index.
    """
    assert len(indices) == len(new_values), \
        'length of indices to update and values to update with must match'
    for (i, new_val) in zip(indices, new_values):
        values[i] = new_val


def main() -> None:
    """Connect to a server and play games using a loaded model."""
    args = parse_args()
    name = args.name
    if name is not None:
        Client.check_name_length(name.encode())

    algorithm = args.algorithm
    checkpoint_path = Path(args.checkpoint_path)
    assert not checkpoint_path.is_dir(), \
        'please pass the checkpoint file, not its directory'

    ray.init()

    with server_utils.create_client() as client:
        client.connect((args.server_address, args.port))
        client.settimeout(SERVER_TIMEOUT_SEC)
        print('Connected to server.')
        server_utils.send_name(client, name)

        metadata = wait_for_data(
            client,
            server_utils.MAX_RECEIVE_BYTES,
            server_utils.MAX_RECEIVE_BYTES,
        )
        player_index = metadata['player_index']
        num_players = metadata['num_players']
        deck_size = metadata['deck_size']
        mask_actions = metadata['mask_actions']
        max_num_games = metadata['max_num_games']
        num_parallel_games = metadata['num_parallel_games']

        print(f'Positioned at index {player_index}.')

        max_total_receive_bytes = \
            server_utils.MAX_RECEIVE_BYTES * num_parallel_games
        # We only get strings as keys.
        str_player_index = str(player_index)

        env_config = {
            'num_players': num_players,
            'deck_size': deck_size,
            'mask_actions': mask_actions,
        }
        obs_space, act_space = utils.get_spaces(ENV_NAME, env_config)

        model_config = {
            'use_lstm': False,
        }

        config = {
            'env': ENV_NAME,
            'env_config': env_config,
            'model': model_config,
            'explore': False,
            'multiagent': {
                'policies_to_train': [],
                'policies': {
                    LEARNED_POLICY_ID: (None, obs_space, act_space, {}),
                    'random': (RandomPolicy, obs_space, act_space,
                               {'mask_actions': mask_actions}),
                    'rulebased': (RuleBasedPolicy, obs_space, act_space,
                                  {'mask_actions': mask_actions}),
                },
                'policy_mapping_fn': lambda _: LEARNED_POLICY_ID,
            },
            'num_gpus': utils.get_num_gpus(args.framework),
            'num_workers': 0,
            'framework': args.framework,
        }
        utils.maybe_set_up_masked_actions_model(algorithm, config)

        agent = utils.load_agent(algorithm, str(checkpoint_path), config)
        server_utils.send_ok(client)

        num_iters = 0
        num_games = 0
        while not _is_done(num_games, max_num_games):
            states: List[TensorType] = [
                utils.get_initial_state(agent, LEARNED_POLICY_ID)
                for _ in range(num_parallel_games)
            ]
            prev_actions: List[Optional[TensorType]] = \
                [None] * num_parallel_games
            prev_rewards: List[Optional[Reward]] = \
                [None] * num_parallel_games

            while True:
                data = wait_for_data(
                    client,
                    server_utils.MAX_RECEIVE_BYTES,
                    max_total_receive_bytes,
                )

                if len(data) == 0:
                    # We have no observations; send no actions.
                    server_utils.send_actions(client, [])

                if len(data[0]) < 4:
                    (indices, obss) = zip(*data)
                else:
                    (indices, obss, rewards, is_dones, infos) = zip(*data)
                    rewards = [
                        reward[str_player_index]
                        for reward in rewards
                    ]
                    _update_indices(prev_rewards, indices, rewards)

                    if is_dones[0]['__all__']:
                        break
                assert all(str_player_index in obs for obs in obss), \
                    'received wrong data'
                obss = [obs[str_player_index] for obs in obss]
                # print('Received', len(obss), 'observations.')

                masked_prev_actions = _take_indices(prev_actions, indices)
                masked_prev_rewards = _take_indices(prev_rewards, indices)
                actions, new_states, _ = utils.compute_actions(
                    agent,
                    obss,
                    _take_indices(states, indices),
                    (
                        masked_prev_actions
                        if None not in masked_prev_actions
                        else None
                    ),
                    (
                        masked_prev_rewards
                        if None not in masked_prev_rewards
                        else None
                    ),
                    policy_id=LEARNED_POLICY_ID,
                    full_fetch=True,
                )
                # print('Actions:', actions)

                server_utils.send_actions(client, actions)

                _update_indices(states, indices, new_states)
                _update_indices(prev_actions, indices, actions)

            server_utils.send_ok(client)
            num_games += num_parallel_games
            num_iters += 1

            if num_iters % 100 == 0:
                print('Played', num_games, 'games.')

    ray.shutdown()


if __name__ == '__main__':
    main()
