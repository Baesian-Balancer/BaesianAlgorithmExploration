import numpy as np
import scipy.signal
from gym.spaces import Box, Discrete

import torch
import torch.nn as nn
import torch.distributions as ptd
from torch.distributions.normal import Normal
from torch.distributions.categorical import Categorical
import torch.nn.functional as functional
import distributions


def combined_shape(length, shape=None):
    if shape is None:
        return (length,)
    return (length, shape) if np.isscalar(shape) else (length, *shape)


def mlp(sizes, activation, output_activation=nn.Identity):
    layers = []
    for j in range(len(sizes)-1):
        act = activation if j < len(sizes)-2 else output_activation
        layers += [nn.Linear(sizes[j], sizes[j+1]), act()]
    return nn.Sequential(*layers)


def count_vars(module):
    return sum([np.prod(p.shape) for p in module.parameters()])


def discount_cumsum(x, discount):
    """
    magic from rllab for computing discounted cumulative sums of vectors.

    input:
        vector x,
        [x0,
         x1,
         x2]

    output:
        [x0 + discount * x1 + discount^2 * x2,
         x1 + discount * x2,
         x2]
    """
    return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]


class Actor(nn.Module):

    def _distribution(self, obs):
        raise NotImplementedError

    def _log_prob_from_distribution(self, pi, act):
        raise NotImplementedError

    def forward(self, obs, act=None):
        # Produce action distributions for given observations, and
        # optionally compute the log likelihood of given actions under
        # those distributions.
        pi = self._distribution(obs)
        logp_a = None
        if act is not None:
            logp_a = self._log_prob_from_distribution(pi, act)
        return pi, logp_a

class MLPGaussianActor(Actor):

    def __init__(self, obs_dim, act_dim, hidden_sizes, activation):
        super().__init__()
        log_std = -0.5 * np.ones(act_dim, dtype=np.float32)
        self.log_std = torch.nn.Parameter(torch.as_tensor(log_std))
        # self.mu_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=activation)
        self.mu_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation)

    def _distribution(self, obs):
        if torch.sum(torch.isnan(obs)):
            print(f'got Nan in Obs: {obs}.')
        mu = self.mu_net(obs)
        std = torch.exp(self.log_std)
        return Normal(mu, std)

    def _mean(self,obs):
        mu = self.mu_net(obs)
        return mu

    def _log_prob_from_distribution(self, pi, act):
        return pi.log_prob(act).sum(axis=-1)    # Last axis sum needed for Torch Normal distribution

class MLPGaussianSquashedActor(Actor):

    def __init__(self, obs_dim, act_dim, hidden_sizes, activation):
        super().__init__()
        log_std = -0.5 * np.ones(act_dim, dtype=np.float32)
        self.log_std = torch.nn.Parameter(torch.as_tensor(log_std))
        self.mu_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation)
        self.pi = distributions.SquashedDiagGaussianDistribution(act_dim)

    def _distribution(self, obs):
        if torch.sum(torch.isnan(obs)):
            print(f'got Nan in Obs: {obs}.')
        mu = self.mu_net(obs)
        return self.pi.proba_distribution(mu, self.log_std)

    def _mean(self,obs):
        mu = self.mu_net(obs)
        return mu

    def _log_prob_from_distribution(self, pi, act):
        return pi.log_prob(act).sum(axis=-1)    # Last axis sum needed for Torch Normal distribution

class MLPBetaActor(Actor):

    def __init__(self, obs_dim, act_dim, hidden_sizes, activation):
        super().__init__()
        self.alpha_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=lambda : nn.ELU())
        self.beta_net = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=lambda : nn.ELU())
    def _distribution(self, obs):
        if torch.sum(torch.isnan(obs)):
            print(f'got Nan in Obs: {obs}.')
        alpha = self.alpha_net(obs) + 2
        beta = self.beta_net(obs) + 2
        return torch.distributions.Beta(alpha, beta)

    def _mean(self,obs):
        alpha = self.alpha_net(obs) + 2
        beta = self.beta_net(obs) + 2
        return alpha / (alpha + beta)

    def _log_prob_from_distribution(self, pi, act):
        return pi.log_prob(act).sum(axis=-1)    # Last axis sum needed for Torch Normal distribution

class MLPCritic(nn.Module):

    def __init__(self, obs_dim, hidden_sizes, activation):
        super().__init__()
        self.v_net = mlp([obs_dim] + list(hidden_sizes) + [1], activation)

    def forward(self, obs):
        return torch.squeeze(self.v_net(obs), -1) # Critical to ensure v has right shape.


class MLPActorCritic(nn.Module):

    def __init__(self, observation_space, action_space,
                 hidden_sizes=(64,64), activation=nn.Tanh):
        super().__init__()

        obs_dim = observation_space.shape[0]

        # policy builder depends on action space
        # self.pi = MLPGaussianActor(obs_dim, action_space.shape[0], hidden_sizes, activation)
        # self.pi = MLPGaussianSquashedActor(obs_dim, action_space.shape[0], hidden_sizes, activation)
        self.pi = MLPBetaActor(obs_dim, action_space.shape[0], hidden_sizes, activation)


        # build value function
        self.v  = MLPCritic(obs_dim, hidden_sizes, activation)

    def step(self, obs, eval=False, std_mu=-1.):
        with torch.no_grad():
            if eval:
                a = self.pi._mean(obs)
                a = torch.clamp(a, min=-1, max=1)
                return a.numpy()
            else:
                pi = self.pi._distribution(obs)
                a = pi.sample()
                a = torch.clamp(a, min=-1, max=1)
                logp_a = self.pi._log_prob_from_distribution(pi, a)
                mu = self.pi._mean(obs)
                v = self.v(obs)
                mu_bar = mu
                if std_mu > 0:
                    mu_bar = self.pi._mean(torch.normal(obs, std_mu))
                return a.numpy(), v.numpy(), logp_a.numpy(), mu.numpy(), mu_bar.numpy()

    def act(self, obs):
        return self.step(obs)[0]
