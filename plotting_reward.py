import pandas as pd
import numpy as np
import glob
import os
import matplotlib.pyplot as plt

path = os.getcwd()



# use os.walk(path) on the main path to get ALL subfolders inside path
for root,dirs,_ in os.walk(path):
    for d in dirs:
        path_sub = os.path.join(root,d) # this is the current subfolder
        for filename in glob.glob(os.path.join(path_sub, '*.csv')):
            df = pd.read_csv(filename)
            plt.plot(df['timesteps_total'],df['episode_reward_mean'])
            plt.xlabel('Total Time Steps')
            plt.ylabel('Epsiode Reward Mean')
            #plt.legend([filename[70:80]])
            plt.show()
            #df.plot(x='timesteps_total',y='episode_reward_mean')
            name = os.path.split(filename)[1] # get the name of the current csv file
            #data_splitter(df, name)
