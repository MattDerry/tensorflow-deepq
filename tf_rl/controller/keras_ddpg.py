import numpy as np
import random
import math
import time
import tensorflow as tf
from keras.models import Sequential
from keras.layers.core import Dense, Activation
from keras.utils import np_utils
from keras import backend as K
from keras import callbacks

import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm
from matplotlib.ticker import MaxNLocator

from tf_rl.models import KERASMLP

from collections import deque

LOG_FILE_DIR = './logs/pendulum_'
FILE_EXT = '.png'

class KerasDDPG(object):
    def __init__(self, observation_size,
                       action_size,
                       actor,
                       critic,
                       exploration_sigma=0.001,
                       exploration_period=10000,
                       store_every_nth=5,
                       train_every_nth=5,
                       minibatch_size=32,
                       discount_rate=0.99,
                       max_experience=100000,
                       target_actor_update_rate=0.01,
                       target_critic_update_rate=0.01):
        """Initialized the Deepq object.

        Based on:
            https://www.cs.toronto.edu/~vmnih/docs/dqn.pdf

        Parameters
        -------
        observation_size : int
            length of the vector passed as observation
        action_size : int
            length of the vector representing an action
        observation_to_actions: dali model
            model that implements activate function
            that can take in observation vector or a batch
            and returns scores (of unbounded values) for each
            action for each observation.
            input shape:  [batch_size, observation_size]
            output shape: [batch_size, action_size]
        exploration_sigma: float (0 to 1)
        exploration_period: int
            probability of choosing a random
            action (epsilon form paper) annealed linearly
            from 1 to exploration_sigma over
            exploration_period
        store_every_nth: int
            to further decorrelate samples do not all
            transitions, but rather every nth transition.
            For example if store_every_nth is 5, then
            only 20% of all the transitions is stored.
        train_every_nth: int
            normally training_step is invoked every
            time action is executed. Depending on the
            setup that might be too often. When this
            variable is set set to n, then only every
            n-th time training_step is called will
            the training procedure actually be executed.
        minibatch_size: int
            number of state,action,reward,newstate
            tuples considered during experience reply
        dicount_rate: float (0 to 1)
            how much we care about future rewards.
        max_experience: int
            maximum size of the reply buffer
        target_actor_update_rate: float
            how much to update target critci after each
            iteration. Let's call target_critic_update_rate
            alpha, target network T, and network N. Every
            time N gets updated we execute:
                T = (1-alpha)*T + alpha*N
        target_critic_update_rate: float
            analogous to target_actor_update_rate, but for
            target_critic
        """
        # memorize arguments
        self.observation_size = observation_size
        self.action_size = action_size

        self.actor = actor
        self.critic = critic
        self.target_actor = self.actor.copy()
        self.target_critic = self.critic.copy()

        self.exploration_sigma = exploration_sigma
        self.exploration_period = exploration_period
        self.store_every_nth = store_every_nth
        self.train_every_nth = train_every_nth
        self.minibatch_size = minibatch_size
        self.discount_rate = discount_rate
        self.learning_rate = 0.001
        self.max_experience = max_experience
        self.training_steps = 0

        self.target_actor_update_rate = target_actor_update_rate
        self.target_critic_update_rate = target_critic_update_rate

        # deepq state
        self.actions_executed_so_far = 0
        self.experience = deque()

        self.iteration = 0

        self.number_of_times_store_called = 0
        self.number_of_times_train_called = 0

        # Orstein-Uhlenbeck Process (temporally correlated noise for exploration)
        self.noise_mean = 0.0
        self.noise_variance = 0.0
        self.ou_theta = 0.15
        self.ou_sigma = 0.2

        self.minibatch = K.variable(minibatch_size)
        self.policy_learning_rate = K.variable(self.learning_rate)
        self.policy_updates = self.get_policy_updates()

        self.target_actor_lr = K.variable(self.target_actor_update_rate)
        self.target_critic_lr = K.variable(self.target_critic_update_rate)

        self.policy_updater = K.function(inputs=[self.critic.model.get_input(train=False), self.actor.model.get_input(train=False)], outputs=[], updates=self.policy_updates)

        self.target_critic_updates = self.get_target_critic_updates()
        self.target_critic_updater = K.function(inputs=[], outputs=[], updates=self.target_critic_updates)

        self.target_actor_updates = self.get_target_actor_updates()
        self.target_actor_updater = K.function(inputs=[], outputs=[], updates=self.target_actor_updates)

        self.updates = []
        self.network_update = K.function(inputs=[], outputs=[], updates=self.updates)

        self.tensor_board_cb = callbacks.TensorBoard()
        self.bellman_error = []

    def policy_gradients(self):
        c_grads = K.gradients(K.sum(self.critic.model.get_output(train=False)), self.critic.model.get_input(train=False))[0]
        _, _, _, _, action_q_grad = tf.split(1, 5, c_grads)
        p_grads = [K.gradients(K.sum(self.actor.model.get_output(train=False),axis=1), z, grad_ys=action_q_grad)[0] for z in self.actor.model.trainable_weights]
        for i, pg in enumerate(p_grads):
            p_grads[i] = pg / self.minibatch
        return p_grads

    def get_policy_updates(self):
        p_grads = self.policy_gradients()
        policy_updates = []
        for p, g in zip(self.actor.model.trainable_weights, p_grads):
            clipped_g = tf.clip_by_value(g, -1.0, 1.0)
            new_p = p + self.policy_learning_rate * clipped_g
            policy_updates.append((p, new_p))
        return policy_updates

    def get_target_critic_updates(self):
        critic_updates = []
        for tp, p in zip(self.target_critic.model.trainable_weights, self.critic.model.trainable_weights):
            new_p = self.target_critic_lr * p + (1 - self.target_critic_lr) * tp
            critic_updates.append((tp, new_p))
        return critic_updates

    def get_target_actor_updates(self):
        actor_updates = []
        for tp, p in zip(self.target_actor.model.trainable_weights, self.actor.model.trainable_weights):
            new_p = self.target_actor_lr * p + (1 - self.target_actor_lr) * tp
            actor_updates.append((tp, new_p))
        return actor_updates

    @staticmethod
    def linear_annealing(n, total, p_initial, p_final):
        """Linear annealing between p_initial and p_final
        over total steps - computes value at step n"""
        if n >= total:
            return p_final
        else:
            return p_initial - (n * (p_initial - p_final)) / (total)

    def plot_bellman_residual(self, save=False, filename=None):
        fig = plt.figure()
        plt.plot(self.bellman_error)
        plt.ylabel('Bellman Residual')
        plt.xlabel('Training Step')
        if save:
            plt.savefig(filename, dpi=600)
        else:
            plt.show()

    def plot_critic_value_function(self, save=False, filename=None):
        dx, dy = 0.05, 0.05
        y, x = np.mgrid[slice(-3.15, 3.15+dy, dy), slice(-3.15, 3.15+dx, dx)]

        states = np.empty((len(y)*len(y), self.observation_size))
        actions = np.zeros((len(y)*len(y), self.action_size))

        for i in range(len(x)):
            for j in range(len(y)):
                states[i*len(y)+j,0] = x[i,j]
                states[i*len(y)+j,1] = 0
                states[i*len(y)+j,2] = y[i,j]
                states[i*len(y)+j,3] = 0

        q_0v_angle = self.critic(np.concatenate((states, actions), axis=1))
        q_0v_angle_t = self.target_critic(np.concatenate((states, actions), axis=1))

        new_q = np.zeros_like(x)
        new_q_t = np.zeros_like(x)
        for i in range(len(q_0v_angle)):
            new_q[i/len(y), i%len(y)] = q_0v_angle[i]
            new_q_t[i/len(y), i%len(y)] = q_0v_angle_t[i]

        new_q = new_q[:-1, :-1]
        new_q_t = new_q_t[:-1, :-1]

        levels = MaxNLocator(nbins=15).tick_values(new_q.min(), new_q.max())
        levels_t = MaxNLocator(nbins=15).tick_values(new_q_t.min(), new_q_t.max())
        cmap = plt.get_cmap('PiYG')
        fig, (ax0, ax1) = plt.subplots(nrows=2)
        cf = ax0.contourf(x[:-1, :-1] + dx/2., y[:-1, :-1]+dy/2., new_q, levels=levels, cmap=cmap)
        fig.colorbar(cf, ax=ax0)
        ax0.set_title('Critic Q val (joint vels = 0, action = 0)')

        cf_t = ax1.contourf(x[:-1, :-1] + dx/2., y[:-1, :-1]+dy/2., new_q_t, levels=levels_t, cmap=cmap)
        fig.colorbar(cf_t, ax=ax1)
        ax1.set_title('Target Critic Q val (joint vels = 0, action = 0)')

        fig.tight_layout()
        if save:
            plt.savefig(filename, dpi=600)
        else:
            plt.show()

    def plot_actor_policy(self, save=False, filename=None):
        dx, dy = 0.05, 0.05
        y, x = np.mgrid[slice(-3.15, 3.15+dy, dy), slice(-3.15, 3.15+dx, dx)]

        states = np.empty((len(y)*len(y), self.observation_size))

        for i in range(len(x)):
            for j in range(len(y)):
                states[i*len(y)+j,0] = x[i,j]
                states[i*len(y)+j,1] = 0
                states[i*len(y)+j,2] = y[i,j]
                states[i*len(y)+j,3] = 0

        actions = self.actor(states)
        actions_t = self.target_actor(states)

        new_a = np.zeros_like(x)
        new_a_t = np.zeros_like(x)
        for i in range(len(actions)):
            new_a[i/len(y), i%len(y)] = actions[i]
            new_a_t[i/len(y), i%len(y)] = actions_t[i]

        new_a = new_a[:-1, :-1]
        new_a_t = new_a_t[:-1, :-1]

        levels = MaxNLocator(nbins=15).tick_values(new_a.min(), new_a.max())
        levels_t = MaxNLocator(nbins=15).tick_values(new_a_t.min(), new_a_t.max())
        cmap = plt.get_cmap('PiYG')
        fig, (ax0, ax1) = plt.subplots(nrows=2)
        cf = ax0.contourf(x[:-1, :-1] + dx/2., y[:-1, :-1]+dy/2., new_a, levels=levels, cmap=cmap)
        fig.colorbar(cf, ax=ax0)
        ax0.set_title('Actor Policy (joint vels = 0)')

        cf_t = ax1.contourf(x[:-1, :-1] + dx/2., y[:-1, :-1]+dy/2., new_a_t, levels=levels_t, cmap=cmap)
        fig.colorbar(cf_t, ax=ax1)
        ax1.set_title('Target Actor Policy (joint vels = 0)')

        fig.tight_layout()
        if save:
            plt.savefig(filename, dpi=600)
        else:
            plt.show()

    def action(self, observation, dt):
        # assert len(observation.shape) == 1, \
        #        "Action is performed based on single observation."
        action = self.actor(observation)
        self.actions_executed_so_far += 1
        if self.exploration_period > self.actions_executed_so_far:
            # Solution for Ornstein-Uhlenbeck Process found here http://planetmath.org/ornsteinuhlenbeckprocess
            self.noise_mean = 0  # action * np.exp(-self.ou_theta*dt)
            self.noise_variance = self.ou_sigma*self.ou_sigma/self.ou_theta * (1 - np.exp(-2*self.ou_theta*dt))
            # noise_sigma = KerasDDPG.linear_annealing(self.actions_executed_so_far, self.exploration_period, 1.0, self.noise_variance)
            action += np.random.normal(self.noise_mean, self.noise_variance, size=action.shape)
            action = np.clip(action, -1., 1.)
        return action

    def target_action(self, observation):
        action = self.target_actor(observation)
        return action

    def store(self, observation, action, reward, newobservation):
        self.experience.append((observation, action, reward, newobservation))
        if len(self.experience) > self.max_experience:
            self.experience.popleft()

    def training_step(self):
        start_time = time.time()
        if len(self.experience) < 1*self.minibatch_size:
            return

        self.training_steps += 1
        print 'Starting training step %d at %s' % (self.training_steps, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(start_time)))

        # sample experience (need to be a two liner, because deque...)
        samples = random.sample(range(len(self.experience)), self.minibatch_size)
        samples = [self.experience[i] for i in samples]
        time_marker1 = time.time()
        # print 'Minibatch sampling from exp took %f seconds' % (time_marker1 - start_time)

        # batch states
        states = np.empty((len(samples), self.observation_size))
        newstates = np.empty((len(samples), self.observation_size))
        actions = np.zeros((len(samples), self.action_size))
        target_actions = np.zeros((len(samples), self.action_size))
        rewards = np.empty((len(samples),1))
        time_marker2 = time.time()
        # print 'Memory allocation took %f seconds' % (time_marker2 - time_marker1)

        for i, (state, action, reward, newstate) in enumerate(samples):
            states[i] = state
            actions[i] = action
            rewards[i] = reward
            newstates[i] = newstate

        time_marker1 = time.time()
        # print 'Sample enumeration took %f seconds' % (time_marker1 - time_marker2)

        for i, state in enumerate(newstates):
            target_actions[i] = self.target_action(state)

        time_marker2 = time.time()
        # print 'Target action retrieval took %f seconds' % (time_marker2 - time_marker1)

        target_y = rewards + self.discount_rate * self.target_critic(np.concatenate((newstates, target_actions), axis=1))
        assert isinstance(self.critic.model, Sequential)
        time_marker1 = time.time()
        # print 'Target Y val took %f seconds' % (time_marker1 - time_marker2)
        # Update critic
        # self.critic.model.fit(np.concatenate((states, actions), axis=1), target_y, batch_size=len(samples), nb_epoch=1, verbose=0, callbacks=[self.tensor_board_cb])
        history = self.critic.model.fit(np.concatenate((states, actions), axis=1), target_y, batch_size=len(samples), nb_epoch=1, verbose=0)
        self.bellman_error.append(history.history['loss'][0])

        time_marker2 = time.time()
        print 'Critic model fitting took %f seconds' % (time_marker2 - time_marker1)
        # Update actor policy
        policy_actions = np.zeros((len(samples), self.action_size))
        for i, state in enumerate(states):
            policy_actions[i] = self.actor(state)

        time_marker1 = time.time()
        # print 'Action retrieval took %f seconds' % (time_marker1 - time_marker2)

        critic_xs = np.matrix(np.concatenate((states, policy_actions), axis=1))
        actor_xs = np.matrix(states)
        self.policy_updater([critic_xs, actor_xs])

        time_marker2 = time.time()
        print 'Policy gradient and update calcs took %f seconds' % (time_marker2 - time_marker1)

        self.target_critic_updater([])
        self.target_actor_updater([])

        time_marker1 = time.time()
        print 'Target network updates took %f seconds' % (time_marker1 - time_marker2)
        print '--------------------------------------'
        print 'Total time spent in training iterations was %f seconds' % (time_marker1 - start_time)
        time.sleep(1.0)

        if self.training_steps % 100 == 1:
            self.plot_critic_value_function(save=True, filename='%s' % (LOG_FILE_DIR + 'critic_' + str(self.training_steps) + FILE_EXT))
            self.plot_actor_policy(save=True, filename='%s' % (LOG_FILE_DIR + 'policy_' + str(self.training_steps) + FILE_EXT))
            self.plot_bellman_residual(save=True, filename='%s' % (LOG_FILE_DIR + 'bellman_residual_' + str(self.training_steps) + FILE_EXT))








