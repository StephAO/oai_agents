from oai_agents.common.state_encodings import ENCODING_SCHEMES
from oai_agents.common.arguments import get_args_to_save, set_args_from_load

from overcooked_ai_py.mdp.overcooked_mdp import Action

from abc import ABC, abstractmethod
import argparse
from pathlib import Path
import numpy as np
import torch as th
import torch.nn as nn
from typing import List, Tuple, Union
import stable_baselines3.common.distributions as sb3_distributions
from stable_baselines3.common.evaluation import evaluate_policy
import wandb


class OAIAgent(nn.Module, ABC):
    """
    A smaller version of stable baselines Base algorithm with some small changes for my new agents
    https://stable-baselines3.readthedocs.io/en/master/modules/base.html#stable_baselines3.common.base_class.BaseAlgorithm
    Ensures that all agents play nicely with the environment
    """
    def __init__(self, name, args):
        super(OAIAgent, self).__init__()
        self.name = name
        # Player index and Teammate index
        self.encoding_fn = ENCODING_SCHEMES[args.encoding_fn]
        self.args = args
        # Must define a policy. The policy must implement a get_distribution(obs) that returns the action distribution
        self.policy = None
        self.p_idx = None

    @abstractmethod
    def predict(self, obs: th.Tensor, state=None, episode_start=None, deterministic: bool=False) -> Tuple[int, Union[th.Tensor, None]]:
        """
        Given an observation return the index of the action and the agent state if the agent is recurrent.
        Structure should be the same as agents created using stable baselines:
        https://stable-baselines3.readthedocs.io/en/master/modules/base.html#stable_baselines3.common.base_class.BaseAlgorithm.predict
        """

    @abstractmethod
    def get_distribution(self, obs: th.Tensor) -> Union[th.distributions.Distribution, sb3_distributions.Distribution]:
        """
        Given an observation return the index of the action and the agent state if the agent is recurrent.
        Structure should be the same as agents created using stable baselines:
        https://stable-baselines3.readthedocs.io/en/master/modules/base.html#stable_baselines3.common.base_class.BaseAlgorithm.predict
        """

    def set_idx(self, p_idx):
        self.p_idx = p_idx

    def action(self, overcooked_state):
        if self.p_idx is None:
            raise ValueError('Please call set_idx() before action. Otherwise, call predict with agent specific obs')
        obs = self.encoding_fn(overcooked_state, p_idx=self.p_idx)
        action, _ = self.predict(obs, deterministic=True)
        return Action.INDEX_TO_ACTION[action]

    def _get_constructor_parameters(self):
        return dict(name=self.name, args=self.args)

    def step(self):
        pass

    def reset(self):
        pass

    def save(self, path: str) -> None:
        """
        Save model to a given location.
        :param path:
        """
        args = get_args_to_save(self.args)
        th.save({'agent_type': type(self), 'state_dict': self.state_dict(),
                 'const_params': self._get_constructor_parameters(), 'args': args}, path)

    @classmethod
    def load(cls, path: str, args: argparse.Namespace) -> 'OAIAgent':
        """
        Load model from path.
        :param path: path to save to
        :param device: Device on which the policy should be loaded.
        :return:
        """
        device = args.device
        saved_variables = th.load(path, map_location=device)
        set_args_from_load(saved_variables['args'], args)
        saved_variables['const_params']['args'] = args
        # Create agent object
        model = cls(**saved_variables['const_params'])  # pytype: disable=not-instantiable
        # Load weights
        model.load_state_dict(saved_variables['state_dict'])
        model.to(device)
        return model

class SB3Wrapper(OAIAgent):
    def __init__(self, agent, name, args):
        super(SB3Wrapper, self).__init__(name, args)
        self.agent = agent
        self.policy = self.agent.policy
        self.num_timesteps = 0

    def predict(self, obs, state=None, episode_start=None, deterministic=False):
        return self.agent.predict(obs, state=state, episode_start=episode_start, deterministic=deterministic)

    def get_distribution(self, obs: th.Tensor):
        return self.agent.get_distribution(obs)

    def learn(self, total_timesteps):
        self.agent.learn(total_timesteps=total_timesteps, reset_num_timesteps=False)
        self.num_timesteps = self.agent.num_timesteps

    def save(self, path: Path) -> None:
        """
        Save model to a given location.
        :param path:
        """
        save_path = path / 'agent_file'
        args = get_args_to_save(self.args)
        th.save({'agent_type': type(self), 'sb3_model_type': type(self.agent),
                 'const_params': self._get_constructor_parameters(), 'args': args}, save_path)
        self.agent.save(str(save_path) + '_sb3_agent')

    @classmethod
    def load(cls, path: Path, args: argparse.Namespace, **kwargs) -> 'SB3Wrapper':
        """
        Load model from path.
        :param path: path to save to
        :param device: Device on which the policy should be loaded.
        :return:
        """
        device = args.device
        load_path = path / 'agent_file'
        saved_variables = th.load(load_path)
        set_args_from_load(saved_variables['args'], args)
        saved_variables['const_params']['args'] = args
        # Create agent object
        agent = saved_variables['sb3_model_type'].load(str(load_path) + '_sb3_agent' )
        # Create wrapper object
        model = cls(agent=agent, **saved_variables['const_params'], **kwargs)  # pytype: disable=not-instantiable
        model.to(device)
        return model

class SB3LSTMWrapper(SB3Wrapper):
    ''' A wrapper for a stable baselines 3 agents that uses an lstm and controls a single player '''
    def __init__(self, agent, name, args):
        super(SB3LSTMWrapper, self).__init__(agent, name, args)
        self.lstm_states = None

    def predict(self, obs, state=None, episode_start=None, deterministic=False):
        # TODO pass in episode starts
        episode_start = episode_start or np.ones((1,), dtype=bool)
        action, self.lstm_states = self.agent.predict(obs, state=state, episode_start=episode_start,
                                                      deterministic=deterministic)
        return action, self.lstm_states

    def get_distribution(self, obs: th.Tensor):
        return self.agent.get_distribution(obs)


class OAITrainer(ABC):
    """
    An abstract base class for trainer classes.
    Trainer classes must have two agents that they can train using some paradigm
    """
    def __init__(self, name, args, seed=None):
        super(OAITrainer, self).__init__()
        self.name = name
        self.args = args
        self.ck_list = []
        if seed is not None:
            th.manual_seed(seed)

    def _get_constructor_parameters(self):
        return dict(name=self.name, args=self.args)

    def evaluate(self, eval_agent, eval_teammate, num_episodes=10, visualize=False, timestep=None):
        if visualize and not self.eval_env.visualization_enabled:
            self.eval_env.setup_visualization()
        self.eval_env.set_teammate(eval_teammate)
        mean_reward, std_reward = evaluate_policy(eval_agent, self.eval_env, n_eval_episodes=num_episodes, warn=False)
        timestep = timestep or eval_agent.num_timesteps
        print(f'Eval at timestep {timestep}: {mean_reward}')
        wandb.log({'eval_mean_reward': mean_reward, 'timestep': timestep})
        return mean_reward

    def set_new_teammates(self):
        for i in range(self.args.n_envs):
            self.env.env_method('set_teammate', self.teammates[np.random.randint(len(self.teammates))], indices=i)

    def get_agents(self) -> List[OAIAgent]:
        """
        Structure should be the same as agents created using stable baselines:
        https://stable-baselines3.readthedocs.io/en/master/modules/base.html#stable_baselines3.common.base_class.BaseAlgorithm.predict
        """
        return self.agents

    def save_agents(self, path: Union[Path, None] = None, tag: Union[Path, None] = None):
        ''' Saves each agent that the trainer is training '''
        path = path or self.args.base_dir / 'agent_models' / self.name / self.args.layout_name
        tag = tag or self.args.exp_name
        save_path = path / tag / 'trainer_file'
        agent_path = path / tag / 'agents_dir'
        Path(agent_path).mkdir(parents=True, exist_ok=True)
        save_dict = {'model_type': type(self.agents[0]), 'agent_fns': []}
        for i, agent in enumerate(self.agents):
            agent_path_i = agent_path / f'agent_{i}'
            agent.save(agent_path_i)
            save_dict['agent_paths'].append(f'agent_{i}')
        th.save(save_dict, save_path)
        return path, tag

    def load_agents(self, path: Union[Path, None]=None, tag: Union[Path, None]=None):
        ''' Loads each agent that the trainer is training '''
        path = path or self.args.base_dir / 'agent_models' / self.name / self.args.layout_name
        tag = tag or self.args.exp_name
        load_path = path / tag / 'trainer_file'
        agent_path = path / tag / 'agents_dir'
        device = self.args.device
        saved_variables = th.load(load_path, map_location=device)

        # Load weights
        agents = []
        for agent_fn in saved_variables['agent_fns']:
            agent = saved_variables['model_type'].load(agent_path / agent_fn, self.args)
            agent.to(device)
            agents.append(agent)
        self.agents = agents
        return self.agents