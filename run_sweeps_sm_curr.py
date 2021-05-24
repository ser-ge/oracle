# Environment
import random

import gym
import gym_minigrid
import torch
import numpy as np
import time
from itertools import product
from tqdm import tqdm

import yaml
import math
import pprint
import pickle

from pathlib import Path
from agents.BaselineAgent import BaselineAgent, BaselineAgentExpMem
from agents.BrainAgent import Agent, AgentMem, AgentExpMem

from models.BaselineModel import BaselineModel, BaselineModelExpMem
from models.BrainModel import BrainNet, BrainNetMem, BrainNetExpMem

from oracle.oracle import OracleWrapper
from utils.Trainer import train_test

from language_model import Dataset, Model as QuestionRNN
from oracle.generator import gen_phrases
from typing import Callable
from dataclasses import dataclass, asdict

import wandb

@dataclass
class Config:
    train: bool = True
    epochs: int = 30
    batch_size: int = 256
    sequence_len: int = 10
    lstm_size: int = 128
    word_embed_dims: int = 128
    drop_out_prob: float = 0
    gen_phrases: Callable = gen_phrases
    hidden_dim: float = 32
    lr: float = 0.0005
    gamma: float = 0.99
    lmbda: float = 0.95
    clip: float = 0.2
    entropy_act_param: float = 0.1
    value_param: float = 1
    policy_qa_param: float = 0.25
    advantage_qa_param: float = 0.25
    entropy_qa_param: float = 0.05
    train_episodes: float = 5000
    test_episodes: float = 5000
    train_log_interval: float = 3
    test_log_interval: float = 1
    log_questions: bool = False
    train_env_name: str =  "MiniGrid-RedBlueDoors-6x6-v0"
    test_env_name: str = "MiniGrid-RedBlueDoors-8x8-v0"
    #, "MiniGrid-MultiRoom-N4-S5-v0" "MiniGrid-Empty-8x8-v0"
    ans_random: float = 0
    undefined_error_reward: float = 0
    syntax_error_reward: float = -0.2
    defined_q_reward: float = 0.2
    defined_q_reward_test : float = 0
    pre_trained_lstm: bool = True
    use_seed: bool = False
    seed: int = 1
    use_mem: bool = True
    exp_mem: bool = True
    baseline: bool = False
    wandb: bool = True




# def load_yaml_config(path_to_yaml)
#     try:
#         with open (path_to_yaml, 'r') as file:
#         config = yaml.safe_load(file)
#     except Exception as e:
#     print('Error reading the config file')


default_config = Config()
# yaml_config = load_yaml_config("./config.yaml")
# yaml_config = Config(**yaml_config)

device = "cpu"

USE_WANDB = default_config.wandb
NUM_RUNS = 2
RUNS_PATH = Path('./data')


sweep_config = {
    "name" : "Curr",
    "method": "random",
    "metric": {"name": "eps_reward", "goal": "maximize"},

    "parameters": {
        # "train_env_name" : {
        #     'value' : 'MiniGrid-KeyCorridorS3R1-v0'
        #     },

        "baseline" : {
            'values' : [True, False]
            },

    }
}

class Logger:
    def log(self, *args):
        pass


def run_experiments(configs=sweep_config, num_runs=NUM_RUNS, runs_path=RUNS_PATH):
    """
    pickle.load(open('data/run_results_Thu Apr 29 13:14:39 2021.p', 'rb'))
    """
    save_path = runs_path / Path('run_results_' + str(time.asctime()) + '.p')

    if USE_WANDB:
        sweep_id = wandb.sweep(configs, project="ask_before_you_act")
        wandb.agent(sweep_id, function=run_experiment)
        return

    else:
        configs = gen_configs(configs)
        experiments = [{'config' : cfg, 'train_rewards':[], 'test_rewards' : []} for cfg in configs]

        print(f"Total of {len(experiments)} experiments collected for {num_runs} runs each")

        for i, exp in enumerate(experiments):
            config = Config(**exp['config'])
            pprint.pprint(asdict(config))
            print(f"Running Experiment {i}")
            for run in tqdm(range(num_runs)):
                train_reward, test_reward = run_experiment(config)
                exp['train_rewards'].append(train_reward)
                exp['test_rewards'].append(test_reward)

        pickle.dump(experiments, open(save_path, 'wb'))
        print(f"Run results saved to {save_path}")
        return experiments

def run_experiment(cfg=default_config):
    dataset = Dataset(cfg)
    question_rnn = QuestionRNN(dataset, cfg)

    if USE_WANDB:
        run = wandb.init(project='ask_before_you_act', config=asdict(cfg))
        logger = wandb
        cfg = wandb.config
    else:

        logger = Logger()

    if cfg.pre_trained_lstm:
        question_rnn.load('./language_model/pre-trained.pth')

    env_train = gym.make(cfg.train_env_name)
    env_test = gym.make(cfg.test_env_name)

    if cfg.use_seed:
        env_test.seed(cfg.seed)
        env_train.seed(cfg.seed)
        np.random.seed(cfg.seed)
        torch.manual_seed(cfg.seed)
        random.seed(cfg.seed)

    env_train = OracleWrapper(env_train, syntax_error_reward=cfg.syntax_error_reward,
                              undefined_error_reward=cfg.undefined_error_reward,
                              defined_q_reward=cfg.defined_q_reward,
                              ans_random=cfg.ans_random)

    env_test = OracleWrapper(env_test, syntax_error_reward=cfg.syntax_error_reward,
                              undefined_error_reward=cfg.undefined_error_reward,
                              defined_q_reward=cfg.defined_q_reward_test,
                              ans_random=cfg.ans_random)

    # Agent
    agent = set_up_agent(cfg, question_rnn)

    # Train
    train_reward = train_test(env_train, agent, cfg, logger, n_episodes=cfg.train_episodes,
                              log_interval=cfg.train_log_interval, train=True, verbose=True, test_env=False)

    # Test
    test_reward = train_test(env_test, agent, cfg, logger, n_episodes=cfg.test_episodes,
                              log_interval=cfg.train_log_interval, train=True, verbose=True, test_env=True)

    # test_reward = train_test(env_test, agent, cfg, logger, n_episodes=cfg.test_episodes,
    #                          log_interval=cfg.test_log_interval, train=False, verbose=True)

    if USE_WANDB: run.finish()
    return train_reward, test_reward


def plot_experiment(means, stds, total_runs, window=25):
    import matplotlib.pyplot as plt
    import pandas as pd

    fig, axs = plt.subplots(2, 1)
    if len(means) == 2:  # Baseline
        mu_train_baseline = np.array((window - 1) * [0] + pd.Series(means[0]).rolling(window).mean().to_list()[window - 1:])
        mu_test_baseline = np.array((window - 1) * [0] + pd.Series(means[1]).rolling(window).mean().to_list()[window - 1:])
        std_train_baseline = np.array((window - 1) * [0] + pd.Series(stds[0]).rolling(window).mean().to_list()[window - 1:])
        std_test_baseline = np.array((window - 1) * [0] + pd.Series(stds[1]).rolling(window).mean().to_list()[window - 1:])

        episodes = np.linspace(0, len(mu_train_baseline)-1, num=len(mu_train_baseline))

        axs[0].set_title(f"Reward curves, smoothed over {total_runs} runs")
        axs[0].plot(mu_train_baseline, label="Baseline")
        axs[0].fill_between(episodes, mu_train_baseline - std_train_baseline, mu_train_baseline + std_train_baseline, alpha=0.3)
        axs[0].set_xlabel("Episodes")
        axs[0].set_ylabel("Training reward")
        axs[0].legend()

        axs[1].plot(mu_test_baseline, label="Baseline")
        axs[1].fill_between(episodes, mu_test_baseline - std_test_baseline, mu_test_baseline + std_test_baseline, alpha=0.3)
        axs[1].set_xlabel("Episodes")
        axs[1].set_ylabel("Test reward")
        axs[1].legend()
        plt.show()

    else:
        # Baseline
        mu_train_baseline = np.array((window - 1) * [0] + pd.Series(means[0]).rolling(window).mean().to_list()[window - 1:])
        mu_test_baseline = np.array((window - 1) * [0] + pd.Series(means[1]).rolling(window).mean().to_list()[window - 1:])
        std_train_baseline = np.array((window - 1) * [0] + pd.Series(stds[0]).rolling(window).mean().to_list()[window - 1:])
        std_test_baseline = np.array((window - 1) * [0] + pd.Series(stds[1]).rolling(window).mean().to_list()[window - 1:])

        # Model
        mu_train_model = np.array((window - 1) * [0] + pd.Series(means[2]).rolling(window).mean().to_list()[window - 1:])
        mu_test_model = np.array((window - 1) * [0] + pd.Series(means[3]).rolling(window).mean().to_list()[window - 1:])
        std_train_model = np.array((window - 1) * [0] + pd.Series(stds[2]).rolling(window).mean().to_list()[window - 1:])
        std_test_model = np.array((window - 1) * [0] + pd.Series(stds[3]).rolling(window).mean().to_list()[window - 1:])

        episodes = np.linspace(0, len(mu_train_baseline) - 1, num=len(mu_train_baseline))

        axs[0].set_title(f"Reward curves, smoothed over {total_runs} runs")
        axs[0].plot(mu_train_baseline, label="Baseline")
        axs[0].fill_between(episodes, mu_train_baseline - std_train_baseline, mu_train_baseline + std_train_baseline,
                            alpha=0.3)
        axs[0].plot(mu_train_model, label="Model")
        axs[0].fill_between(episodes, mu_train_model - std_train_model, mu_train_model + std_train_model, alpha=0.3)
        axs[0].set_xlabel("Episodes")
        axs[0].set_ylabel("Training reward")
        axs[0].legend()

        axs[1].plot(mu_test_baseline, label="Baseline")
        axs[1].fill_between(episodes, mu_test_baseline - std_test_baseline, mu_test_baseline + std_test_baseline,
                            alpha=0.3)
        axs[1].plot(mu_test_model, label="Model")
        axs[1].fill_between(episodes, mu_test_model - std_test_model, mu_test_model + std_test_model, alpha=0.3)
        axs[1].set_xlabel("Episodes")
        axs[1].set_ylabel("Test reward")
        axs[1].legend()
        plt.show()
    # fig.savefig("./figures/figure_run" + str(total_runs) + signature + ".png")

def set_up_agent(cfg, question_rnn):
    if cfg.baseline:
        if cfg.use_mem:
            model = BaselineModelExpMem()
            agent = BaselineAgentExpMem(model, cfg.lr, cfg.lmbda, cfg.gamma, cfg.clip,
                             cfg.value_param, cfg.entropy_act_param)

        else:
            model = BaselineModel()
            agent = BaselineAgent(model, cfg.lr, cfg.lmbda, cfg.gamma, cfg.clip,
                                        cfg.value_param, cfg.entropy_act_param)

    else:
        if cfg.use_mem and cfg.exp_mem:
            model = BrainNetExpMem(question_rnn)
            agent = AgentExpMem(model, cfg.lr, cfg.lmbda, cfg.gamma, cfg.clip,
                                cfg.value_param, cfg.entropy_act_param,
                                cfg.policy_qa_param, cfg.advantage_qa_param,
                                cfg.entropy_qa_param)

        elif cfg.use_mem and not cfg.exp_mem:
            model = BrainNetMem(question_rnn)
            agent = AgentMem(model, cfg.lr, cfg.lmbda, cfg.gamma, cfg.clip,
                             cfg.value_param, cfg.entropy_act_param,
                             cfg.policy_qa_param, cfg.advantage_qa_param,
                             cfg.entropy_qa_param)

        else:
            model = BrainNet(question_rnn)
            agent = Agent(model, cfg.lr, cfg.lmbda, cfg.gamma, cfg.clip,
                          cfg.value_param, cfg.entropy_act_param,
                          cfg.policy_qa_param, cfg.advantage_qa_param,
                          cfg.entropy_qa_param)
    return agent

def gen_configs(sweep_config):
    params = sweep_config['parameters']

    configs = []
    sweep_params = {}
    fixed_params = []

    for param in params:
        if 'values' in params[param].keys():
            sweep_params[param] = params[param]['values']
        else:
            fixed_params.append(param)

    base_config = {param: params[param]['value'] for param in fixed_params}

    for param_values in product(*list(sweep_params.values())):
        cfg = base_config.copy()
        for i, param in enumerate(sweep_params.keys()):
            cfg[param] = param_values[i]

        configs.append(cfg)

    return configs


def run_curriculum():
    global default_config
    train_hist = []
    test_hist = []
    total_runs = 3
    window = 3
    for runs in range(total_runs):
        default_config.baseline = True
        train_reward_baseline, test_reward_baseline = run_experiment(cfg=default_config)
        train_hist.append(train_reward_baseline)
        test_hist.append(test_reward_baseline)

    mean_train_baseline = np.array(train_hist).mean(axis=0)
    mean_test_baseline = np.array(test_hist).mean(axis=0)
    std_train_baseline = np.array(train_hist).std(axis=0)
    std_test_baseline = np.array(test_hist).std(axis=0)

    plot_experiment([mean_train_baseline, mean_test_baseline],
                    [std_train_baseline, std_test_baseline],
                    total_runs, window)

    for runs in range(total_runs):
        default_config.baseline = False
        train_reward, test_reward = run_experiment(cfg=default_config)
        train_hist.append(train_reward)
        test_hist.append(test_reward)

    mean_train_model = np.array(train_hist).mean(axis=0)
    mean_test_model = np.array(test_hist).mean(axis=0)
    std_train_model = np.array(train_hist).std(axis=0)
    std_test_model = np.array(test_hist).std(axis=0)

    plot_experiment([mean_train_baseline, mean_test_baseline,
                     mean_train_model, mean_test_model],
                    [std_train_baseline, std_test_baseline,
                     std_train_model, std_test_model],
                    total_runs, window)

if __name__ == "__main__":
    # # run_curriculum()
    # run_experiments()
    run_experiments()
