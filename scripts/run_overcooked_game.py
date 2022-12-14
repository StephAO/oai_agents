import json
import numpy as np
import pandas as pd
from pathlib import Path
import pygame
from pygame import K_UP, K_LEFT, K_RIGHT, K_DOWN, K_SPACE, K_s
from pygame.locals import HWSURFACE, DOUBLEBUF, RESIZABLE
import matplotlib
matplotlib.use('TkAgg')
from os import listdir
from os.path import isfile, join
import re

from oai_agents.agents.base_agent import OAIAgent
from oai_agents.agents.il import BehaviouralCloningAgent
from oai_agents.agents.rl import MultipleAgentsTrainer
from oai_agents.agents.hrl import MultiAgentSubtaskWorker
# from oai_agents.agents import Manager
from oai_agents.common.arguments import get_arguments
from oai_agents.common.subtasks import Subtasks, get_doable_subtasks
from oai_agents.gym_environments.base_overcooked_env import OvercookedGymEnv
from oai_agents.agents.agent_utils import DummyAgent
# from oai_agents.gym_environments import OvercookedSubtaskGymEnv
from oai_agents.common.state_encodings import ENCODING_SCHEMES
from overcooked_ai_py.mdp.overcooked_mdp import Direction, Action, OvercookedState, OvercookedGridworld
# from overcooked_ai_py.planning.planners import MediumLevelPlanner
from overcooked_ai_py.visualization.state_visualizer import StateVisualizer
from overcooked_ai_py.planning.planners import MediumLevelActionManager

no_counters_params = {
    'start_orientations': False,
    'wait_allowed': False,
    'counter_goals': [],
    'counter_drop': [],
    'counter_pickup': [],
    'same_motion_goals': True
}

valid_counters = [(5, 3)]
one_counter_params = {
    'start_orientations': False,
    'wait_allowed': False,
    'counter_goals': valid_counters,
    'counter_drop': valid_counters,
    'counter_pickup': [],
    'same_motion_goals': True
}


def str_to_actions(joint_action):
    """
    Convert df cell format of a joint action to a joint action as a tuple of indices.
    Used to convert pickle files which are stored as strings into np.arrays
    """
    try:
        joint_action = json.loads(joint_action)
    except json.decoder.JSONDecodeError:
        # Hacky fix taken from https://github.com/HumanCompatibleAI/human_aware_rl/blob/master/human_aware_rl/human/data_processing_utils.py#L29
        joint_action = eval(joint_action)
    for i in range(2):
        if type(joint_action[i]) is list:
            joint_action[i] = tuple(joint_action[i])
        if type(joint_action[i]) is str:
            joint_action[i] = joint_action[i].lower()
        assert joint_action[i] in Action.ALL_ACTIONS
    return joint_action


def str_to_state(state):
    """
    Convert from a df cell format of a state to an Overcooked State
    Used to convert pickle files which are stored as strings into overcooked states
    """
    if type(state) is str:
        state = json.loads(state)
    return OvercookedState.from_dict(state)

class App:
    """Class to run an Overcooked Gridworld game, leaving one of the agents as fixed.
    Useful for debugging. Most of the code from http://pygametutorials.wikidot.com/tutorials-basic."""
    def __init__(self, args, agent=None, teammate=None, traj_file=None, slowmo_rate=1):
        self._running = True
        self._display_surf = None
        self.args = args
        self.layout_name = 'forced_coordination' # counter_circuit_o_1order,forced_coordination,asymmetric_advantages # args.layout_names[0]

        self.use_subtask_env = False
        if self.use_subtask_env:
            self.p_idx = 0
            self.t_idx = (self.p_idx + 1) % 2
            tm = BehaviouralCloningAgent.load('/home/miguel/Documents/projects/overcooked_ai/agent_models/bc/counter_circuit_o_1order/st3_p2', args)
            p_kwargs = {'p1': tm} if self.t_idx == 0 else {'p2': tm}
            kwargs = {'single_subtask_id': 10, 'shape_rewards': True, 'args': args, 'is_eval_env': False}
            self.env = OvercookedSubtaskGymEnv(**p_kwargs, **kwargs)
            agents = ['human', tm]
        else:
            # self.mdp = OvercookedGridworld.from_layout_name(self.layout_name)
            # all_counters = self.mdp.get_counter_locations()
            # COUNTERS_PARAMS = {
            #     'start_orientations': False,
            #     'wait_allowed': False,
            #     'counter_goals': all_counters,
            #     'counter_drop': all_counters,
            #     'counter_pickup': all_counters,
            #     'same_motion_goals': True
            # }
            # self.mlam = MediumLevelActionManager.from_pickle_or_compute(self.mdp, COUNTERS_PARAMS, force_compute=False)
            # # To ensure that an agent is on both sides of the counter, remove random starts for forced coord
            # ss_fn = self.mdp.get_fully_random_start_state_fn(self.mlam)
            self.env = OvercookedGymEnv(layout_name=self.layout_name, args=args, ret_completed_subtasks=True)
            print(self.env.mdp.get_valid_player_positions())
            # teammate if teammate is not None else agent
            self.env.set_teammate(teammate or agent)

        # ss_kwargs = {'p_idx': 0, 'random_pos': True, 'random_dir': True,
        #              'curr_subtask': 0, 'max_random_objs': 5}
        # self.env.env.reset(start_state_kwargs=ss_kwargs)

        self.grid_shape = self.env.grid_shape
        if traj_file is not None:
            self.mode = 'replay'
        elif agent is not None:
            self.mode = 'play'
            self.encoding_fn = ENCODING_SCHEMES[args.encoding_fn]
        else:
            self.mode = 'collect_data'
        self.agent = agent
        self.slowmo_rate = slowmo_rate
        self.fps = 30 // slowmo_rate
        self.score = 0
        self.curr_tick = 0
        self.joint_action = [None, None]
        self.data_path = args.base_dir / args.data_path
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.collect_trajectory = not bool(traj_file)
        if self.collect_trajectory:
            self.trajectory = []
            trial_file = re.compile('^.*\.[0-9]+\.pickle$')
            trial_ids = []
            for file in listdir(self.data_path):
                if isfile(join(self.data_path, file)) and trial_file.match(file):
                    trial_ids.append(int(file.split('.')[-2]))
            self.trial_id = max(trial_ids) + 1 if len(trial_ids) > 0 else 1
        else:
            self.trajectory = pd.read_pickle(data_path / traj_file) if traj_file else []
            self.trajectory = self.trajectory[self.trajectory['layout_name'] == layout_name]

    def on_init(self):
        pygame.init()
        surface = StateVisualizer().render_state(self.env.state, grid=self.env.env.mdp.terrain_mtx)
        self.window = pygame.display.set_mode(surface.get_size(), HWSURFACE | DOUBLEBUF | RESIZABLE)
        self.window.blit(surface, (0, 0))
        pygame.display.flip()
        self._running = True

    def on_event(self, event, pidx):
        if event.type == pygame.KEYDOWN:
            pressed_key = event.dict['key']
            action = None

            if pressed_key == K_UP:
                action = Direction.NORTH
            elif pressed_key == K_RIGHT:
                action = Direction.EAST
            elif pressed_key == K_DOWN:
                action = Direction.SOUTH
            elif pressed_key == K_LEFT:
                action = Direction.WEST
            elif pressed_key == K_SPACE:
                action = Action.INTERACT
            elif pressed_key == K_s:
                action = Action.STAY
            else:
                action = Action.STAY
            self.joint_action[pidx] = Action.ACTION_TO_INDEX[action]

        if event.type == pygame.QUIT:
            self._running = False

    def step_env(self, joint_action):
        prev_state = self.env.state

        if self.use_subtask_env:
            obs, reward, done, info = self.env.step(joint_action[self.p_idx])
        else:
            obs, reward, done, info = self.env.step(joint_action)
        new_state = self.env.state
        # prev_state, joint_action, info = super(OvercookedPsiturk, self).apply_actions()

        # Log data to send to psiturk client
        curr_reward = sum(info['sparse_r_by_agent'])
        self.score += curr_reward
        transition = {
            "state" : json.dumps(prev_state.to_dict()),
            "joint_action" : joint_action,#json.dumps(joint_action.item()),
            "reward" : curr_reward,
            "time_left" : max((1200 - self.curr_tick) / self.fps, 0),
            "score" : self.score,
            "time_elapsed" : self.curr_tick / self.fps,
            "cur_gameloop" : self.curr_tick,
            "layout" : self.env.env.mdp.terrain_mtx,
            "layout_name" : self.layout_name,
            "trial_id" : 100 # TODO this is just for testing self.trial_id,
            # "player_0_id" : self.agents[0],
            # "player_1_id" : self.agents[1],
            # "player_0_is_human" : self.agents[0] in self.human_players,
            # "player_1_is_human" : self.agents[1] in self.human_players
        }
        if self.collect_trajectory:
            self.trajectory.append(transition)
        return done

    def on_loop(self):
        assert(all([action is not None for action in self.joint_action]))
        done = self.step_env(self.joint_action)
        self.joint_action = [None, None]
        self.curr_tick += 1

        if done:
            self._running = False

    def on_render(self, pidx=None):
        p0_action = Action.ACTION_TO_INDEX[self.joint_action[0]] if pidx == 1 else None
        surface = StateVisualizer().render_state(self.env.state, grid=self.env.env.mdp.terrain_mtx, pidx=pidx, hud_data={"timestep": self.curr_tick}, p0_action=p0_action)
        self.window = pygame.display.set_mode(surface.get_size(), HWSURFACE | DOUBLEBUF | RESIZABLE)
        self.window.blit(surface, (0, 0))
        pygame.display.flip()
        # save = input('press y to save')
        # if save.lower() == 'y':
        #     pygame.image.save(self.window, "screenshot.png")

    def on_cleanup(self):
        pygame.quit()

    def collection_execution(self):
        if self.on_init() == False:
            self._running = False
        while (self._running):
            self.joint_action = [None, None]
            for i in range(2):
                self.on_render() #pidx=i)
                while self.joint_action[i] is None:
                    for event in pygame.event.get():
                        self.on_event(event, i)
                    pygame.event.pump()
            self.on_loop()

        self.save_trajectory()
        self.on_cleanup()
        print(f'Trial finished in {self.curr_tick} steps with total reward {self.score}')

    def replay_execution(self):
        assert self.trajectory is not None
        if self.on_init() == False:
            self._running = False
        sleep_time = 1000 // self.fps
        trial_id = self.trajectory.iloc[0]['trial_id']
        for index, row in self.trajectory.iterrows():
            if row['trial_id'] == trial_id and not self.env.is_done():
                self.on_render()
                # assert str_to_state(row['state']) == self.env.state
                pygame.time.wait(sleep_time)
                self.joint_action = str_to_actions(row['joint_action'])
                self.on_loop()
            else:
                self.env.reset()
                print(f'Trial finished in {self.curr_tick} steps with total reward {self.score}')
                trial_id = row['trial_id']
                self.score = 0
                self.curr_tick = 0
        self.on_cleanup()

    def play_execution(self):
        if self.on_init() == False:
            self._running = False
        sleep_time = 1000 // self.fps
        # for i in range(2):
        #     if isinstance(self.agents[i], OAIAgent):
        #         self.agents[i].reset(self.env.state)
        on_reset = True
        while (self._running):
            # self.joint_action = [None, None]
            # for i in range(2):
            #     if self.agent == 'human':
            #
            #         while self.joint_action[i] is None:
            #             for event in pygame.event.get():
            #                 self.on_event(event, i)
            #             pygame.event.pump()
            #     else:
            obs = self.env.get_obs(self.env.p_idx, on_reset=on_reset)
            on_reset = False
            # if type(self.agent) == HumanManagerHRL:
            self.agent_action = self.agent.predict(obs, state=self.env.state)[0]#.squeeze()#.detach().item()
                    # else:
                    #     obs.pop('player_completed_subtasks')
                    #     obs.pop('teammate_completed_subtasks')
                    #     self.joint_action[i] = self.agents[i].predict(obs)[0].squeeze()
            pygame.time.wait(sleep_time)
            self.on_render()

            done = self.step_env(self.agent_action)
            self.curr_tick += 1

            if done:
                self._running = False

            # self.on_loop()

        self.on_cleanup()
        print(f'Trial finished in {self.curr_tick} steps with total reward {self.score}')

    def on_execute(self):
        print(self.mode)
        if self.mode == 'collect_data':
            self.collection_execution()
        elif self.mode == 'replay':
            self.replay_execution()
        elif self.mode == 'play':
            self.play_execution()

    def save_trajectory(self):
        df = pd.DataFrame(self.trajectory)
        df.to_pickle(self.data_path / f'{self.layout_name}.{self.trial_id}.pickle')

    @staticmethod
    def combine_df(data_path):
        trial_file = re.compile('^.*\.[0-9]+\.pickle$')
        df = pd.concat([pd.read_pickle(data_path / f) for f in listdir(data_path) if trial_file.match(f)])
        print(f'Combined df has a length of {len(df)}')
        df.to_pickle(data_path / f'all_trials.pickle')

    @staticmethod
    def fix_files_df(data_path):
        trial_file = re.compile('^.*\.[0-9]+\.pickle$')
        for f in listdir(data_path):
            if trial_file.match(f):
                df = pd.read_pickle(data_path / f)
                def joiner(list_of_lists):
                    for i in range(len(list_of_lists)):
                        list_of_lists[i] = ''.join(list_of_lists[i])
                    return str(list_of_lists)
                df['layout'] = df['layout'].apply(joiner)
                df.to_pickle(data_path / f)



def setup_game(env_name, player_idx):
    agent = None


class HumanManagerHRL(OAIAgent):
    def __init__(self, worker, args):
        super(HumanManagerHRL, self).__init__('hierarchical_rl', args)
        self.worker = worker
        self.curr_subtask_id = 11
        self.prev_pcs = None

    def get_distribution(self, obs, sample=True):
        if obs['player_completed_subtasks'] is not None:
            # Completed previous subtask, set new subtask
            print(f'GOAL: {Subtasks.IDS_TO_SUBTASKS[self.curr_subtask_id]}, DONE: {obs["player_completed_subtasks"]}')
            next_st = input("Enter next subtask (0-10): ")
            self.curr_subtask_id = int(next_st)
        obs['curr_subtask'] = self.curr_subtask_id
        return self.worker.get_distribution(obs, sample=sample)

    def predict(self, obs, state=None, episode_start=None, deterministic: bool=False):
        print(obs['player_completed_subtasks'])
        if np.sum(obs['player_completed_subtasks']) == 1:
            comp_st = np.argmax(obs["player_completed_subtasks"], axis=0)
            print(f'GOAL: {Subtasks.IDS_TO_SUBTASKS[self.curr_subtask_id]}, DONE: {Subtasks.IDS_TO_SUBTASKS[comp_st]}')
            doable_st = [Subtasks.IDS_TO_SUBTASKS[idx] for idx, doable in enumerate(obs['subtask_mask']) if doable == 1]
            print('DOABLE SUBTASKS:', doable_st)
            next_st = input("Enter next subtask (0-10): ")
            self.curr_subtask_id = int(next_st)
        obs['curr_subtask'] = self.curr_subtask_id
        obs.pop('player_completed_subtasks')
        obs.pop('teammate_completed_subtasks')
        return self.worker.predict(obs, state=state, episode_start=episode_start, deterministic=True)



if __name__ == "__main__":
    """
    Sample commands
    -> pbt
    python overcooked_interactive.py -t pbt -r pbt_simple -a 0 -s 8015
    ->
    python overcooked_interactive.py -t ppo -r ppo_sp_simple -s 386
    -> BC
    python overcooked_interactive.py -t bc -r simple_bc_test_seed4
    """

    # parser.add_argument("-t", "--type", dest="type",
    #                     help="type of run, (i.e. pbt, bc, ppo, etc)", required=True)
    # parser.add_argument("-r", "--run_dir", dest="run",
    #                     help="tag of run dir in data/*_runs/", required=True)
    additional_args = [
        ('--combine', {'action': 'store_true', 'help': 'Combine all previous trials'}),
        ('--traj-file', {'type': str, 'default': None, 'help': 'trajectory file to run'}),
        ('--agent-file', {'type': str, 'default': None, 'help': 'agent file to load'}),
    ]
    # parser.add_argument("-no_slowed", "--no_slowed_down", dest="slow",
    #                     help="Slow down time for human to simulate actual test time", action='store_false')
    # parser.add_argument("-s", "--seed", dest="seed", required=False, default=0)
    # parser.add_argument("-a", "--agent_num", dest="agent_num", default=0)
    # parser.add_argument("-i", "--idx", dest="idx", default=0)
    # parser.add_argument('--combine', action='store_true', help='Combine all previous trials')
    # parser.add_argument('--traj-file', type=str, default=None, help='trajectory file to run') # '2019_hh_trials_all.pickle'
    # parser.add_argument('--agent-file', type=str, default=None, help='trajectory file to run')



    args = get_arguments(additional_args)

    # args.layout_names = ['tf_test_4', 'tf_test_4']
    #
    # data_path = args.base_dir / args.data_path
    #
    # mat = MultipleAgentsTrainer(args, num_agents=0)
    # mat.load_agents(path=Path('./agent_models/fcp_pop/ego_pop'), tag='test')
    # teammates = mat.get_agents()
    #
    worker = MultiAgentSubtaskWorker.load(
            Path('./agent_models/multi_agent_subtask_worker/final/'), args)
    #
    hm_hrl = HumanManagerHRL(worker, args)
    #
    # tm = teammates[1]

    dc = App(args, agent=hm_hrl, teammate=DummyAgent('random'), slowmo_rate=8, )
    dc.on_execute()














    # if args.agent_file is not None:
    #     env = OvercookedGymEnv(args=args)
    #     obs = env.get_obs()
    #     visual_obs_shape = obs['visual_obs'][0].shape
    #     agent_obs_shape = obs['agent_obs'][0].shape
    #     agents = [Manager(visual_obs_shape, agent_obs_shape, 0, args),
    #               Manager(visual_obs_shape, agent_obs_shape, 1, args)]
    #     for i, agent in enumerate(agents):
    #         path = args.base_dir / 'agent_models' / 'IL_agents' / (args.agent_file + f'_p{i + 1}')
    #         agent.load(path)
    #
    #     agents[0] = 'human'
    #
    # if args.combine:
    #     App.combine_df(data_path)
    # else:
    #     layout_name = 'asymmetric_advantages'
    #     dc = App(args, traj_file=args.traj_file, agents=agents, slowmo_rate=8, )
    #     dc.on_execute()