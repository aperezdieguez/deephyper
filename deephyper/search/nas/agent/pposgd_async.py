import time
from collections import deque
from pprint import pformat

import numpy as np
import tensorflow as tf
from mpi4py import MPI

import deephyper.search.nas.utils.common.tf_util as U
from deephyper.search import util
from deephyper.search.nas.utils import logger
from deephyper.search.nas.utils.common import (Dataset, explained_variance,
                                               fmt_row, zipsame)
from deephyper.search.nas.utils.common.mpi_adam_async import MpiAdamAsync
from deephyper.search.nas.utils.common.mpi_moments import mpi_moments
from deephyper.search.nas.utils.logging import JsonMessage as jm
from deephyper.search.nas.agent.utils import episode_reward_for_final_timestep

dh_logger = util.conf_logger('deephyper.search.nas.agent.pposgd_async')

def traj_segment_generator(pi, env, horizon, stochastic):
    t = 0
    ac = env.action_space.sample() # not used, just so we have the datatype
    new = True # marks if we're on first timestep of an episode
    ob = env.reset()

    cur_ep_len = 0 # len of current episode
    ep_rets = [] # returns of completed episodes in this segment
    ep_lens = [] # lengths of ...

    ts_i2n_ep = {}

    # Initialize history arrays
    obs = np.array([ob for _ in range(horizon)])
    rews = np.zeros(horizon, 'float32')
    vpreds = np.zeros(horizon, 'float32')
    news = np.zeros(horizon, 'int32')
    acs = np.array([ac for _ in range(horizon)])
    prevacs = acs.copy()

    num_evals = 0

    while True:
        prevac = ac
        ac, vpred = pi.act(stochastic, ob)
        # Slight weirdness here because we need value function at time T
        # before returning segment [0, T-1] so we get the correct
        # terminal value
        if t > 0 and t % horizon == 0:
            while num_evals > 0:
                results = env.get_rewards_ready()
                for (cfg, rew) in results:
                    index = cfg['w']
                    episode_length = ep_lens[ts_i2n_ep[index]-1]
                    episode_rew = episode_reward_for_final_timestep(rews, index, rew, episode_length)
                    num_evals -= 1
                    ep_rets[ts_i2n_ep[index]-1] = episode_rew
            ts_i2n_ep = {}
            data = {"ob" : obs, "rew" : rews, "vpred" : vpreds, "new" : news,
                    "ac" : acs, "prevac" : prevacs, "nextvpred": vpred * (1 - new),
                    "ep_rets" : ep_rets, "ep_lens" : ep_lens}
            yield data
            # Be careful!!! if you change the downstream algorithm to aggregate
            # several of these batches, then be sure to do a deepcopy
            ep_rets = []
            ep_lens = []
        i = t % horizon
        obs[i] = ob
        vpreds[i] = vpred
        news[i] = new
        acs[i] = ac
        prevacs[i] = prevac

        # observ, reward, episode_over, meta -> {}
        ob, rew, new, _ = env.step(ac, i, rank=MPI.COMM_WORLD.Get_rank())
        rews[i] = rew

        cur_ep_len += 1
        if new:
            num_evals += 1
            ts_i2n_ep[i] =  num_evals
            ep_rets.append(0)
            ep_lens.append(cur_ep_len)
            cur_ep_len = 0
            ob = env.reset()
        t += 1

def add_vtarg_and_adv(seg, gamma, lam):
    """
    Compute target value using TD(lambda) estimator, and advantage with GAE(lambda)
    """
    new = np.append(seg["new"], 0) # last element is only used for last vtarg, but we already zeroed it if last new = 1
    vpred = np.append(seg["vpred"], seg["nextvpred"])
    T = len(seg["rew"])
    seg["adv"] = gaelam = np.empty(T, 'float32')
    rew = seg["rew"]
    lastgaelam = 0
    for t in reversed(range(T)):
        nonterminal = 1-new[t+1]
        delta = rew[t] + gamma * vpred[t+1] * nonterminal - vpred[t]
        gaelam[t] = lastgaelam = delta + gamma * lam * nonterminal * lastgaelam
    seg["tdlamret"] = seg["adv"] + seg["vpred"]

def learn(env, policy_fn, *,
        timesteps_per_actorbatch, # timesteps per actor per update
        clip_param, entcoeff, # clipping parameter epsilon, entropy coeff
        optim_epochs, optim_stepsize, optim_batchsize,# optimization hypers
        gamma, lam, # advantage estimation
        max_timesteps=0, max_episodes=0, max_iters=0, max_seconds=0,  # time constraint
        callback=None, # you can do anything in the callback, since it takes locals(), globals()
        adam_epsilon=1e-5,
        schedule='constant' # annealing for stepsize parameters (epsilon and adam)
        ):

    rank = MPI.COMM_WORLD.Get_rank()

    # Setup losses and stuff
    # ----------------------------------------
    ob_space = env.observation_space
    ac_space = env.action_space
    pi = policy_fn("pi", ob_space, ac_space) # Construct network for new policy
    oldpi = policy_fn("oldpi", ob_space, ac_space) # Network for old policy
    atarg = tf.placeholder(dtype=tf.float32, shape=[None]) # Target advantage function (if applicable)
    ret = tf.placeholder(dtype=tf.float32, shape=[None]) # Empirical return

    lrmult = tf.placeholder(name='lrmult', dtype=tf.float32, shape=[]) # learning rate multiplier, updated with schedule
    clip_param = clip_param * lrmult # Annealed cliping parameter epislon

    ob = U.get_placeholder_cached(name="ob")
    ac = pi.pdtype.sample_placeholder([None])

    kloldnew = oldpi.pd.kl(pi.pd)
    ent = pi.pd.entropy()
    meankl = tf.reduce_mean(kloldnew)
    meanent = tf.reduce_mean(ent)
    pol_entpen = (-entcoeff) * meanent

    ratio = tf.exp(pi.pd.logp(ac) - oldpi.pd.logp(ac)) # pnew / pold
    surr1 = ratio * atarg # surrogate from conservative policy iteration
    surr2 = tf.clip_by_value(ratio, 1.0 - clip_param, 1.0 + clip_param) * atarg #
    pol_surr = - tf.reduce_mean(tf.minimum(surr1, surr2)) # PPO's pessimistic surrogate (L^CLIP)
    vf_loss = tf.reduce_mean(tf.square(pi.vpred - ret))
    total_loss = pol_surr + pol_entpen + vf_loss
    losses = [pol_surr, pol_entpen, vf_loss, meankl, meanent]
    loss_names = ["pol_surr", "pol_entpen", "vf_loss", "kl", "ent"]

    var_list = pi.get_trainable_variables()
    lossandgrad = U.function([ob, ac, atarg, ret, lrmult], losses + [U.flatgrad(total_loss, var_list)])
    adam = MpiAdamAsync(var_list, epsilon=adam_epsilon)

    assign_old_eq_new = U.function([],[], updates=[tf.assign(oldv, newv)
        for (oldv, newv) in zipsame(oldpi.get_variables(), pi.get_variables())])
    compute_losses = U.function([ob, ac, atarg, ret, lrmult], losses)

    U.initialize()

    t1 = time.time()
    ##
    adam.sync()
    ##
    t2 = time.time()
    t = t2 - t1
    dh_logger.info(jm(type='adam.sync', rank=rank, duration=t, start_time=t1, end_time=t2))

    if rank == 0: # ZERO is the parameter server
        while True:
            t1 = time.time()
            ## BEGIN - TIMING ##
            rank_worker_source = adam.master_update()
            ## END - TIMING ##
            t2 = time.time()
            t = t2 - t1
            dh_logger.info(jm(type='adam.master_update', rank=rank, duration=t, rank_worker_source=rank_worker_source, start_time=t1, end_time=t2))
    else:
        # Prepare for rollouts
        # ----------------------------------------

        seg_gen = traj_segment_generator(pi, env, timesteps_per_actorbatch, stochastic=True)

        episodes_so_far = 0
        timesteps_so_far = 0
        iters_so_far = 0
        tstart = time.time()
        lenbuffer = deque(maxlen=100) # rolling buffer for episode lengths
        rewbuffer = deque(maxlen=100) # rolling buffer for episode rewards

        cond = sum([max_iters>0, max_timesteps>0, max_episodes>0, max_seconds>0])
        assert cond==1, f"Only one time constraint permitted: cond={cond}, max_iters={max_iters}, max_timesteps={max_timesteps}, max_episodes={max_episodes}, max_seconds={max_seconds}"

        while True:
            if callback: callback(locals(), globals())
            if max_timesteps and timesteps_so_far >= max_timesteps:
                break
            elif max_episodes and episodes_so_far >= max_episodes:
                break
            elif max_iters and iters_so_far >= max_iters:
                break
            elif max_seconds and time.time() - tstart >= max_seconds:
                break

            if schedule == 'constant':
                cur_lrmult = 1.0
            elif schedule == 'linear':
                cur_lrmult =  max(1.0 - float(timesteps_so_far) / max_timesteps, 0)
            else:
                raise NotImplementedError

            logger.log("********** Iteration %i ************"%iters_so_far)

            t1 = time.time()
            ## BEGIN - TIMING ##
            seg = seg_gen.__next__()
            ## END - TIMING ##
            t2 = time.time()
            t = t2 - t1
            dh_logger.info(jm(type='batch_computation', rank=rank, duration=t, start_time=t1, end_time=t2))
            dh_logger.info(jm(type='seg', rank=rank, **seg))

            add_vtarg_and_adv(seg, gamma, lam)

            # ob, ac, atarg, ret, td1ret = map(np.concatenate, (obs, acs, atargs, rets, td1rets))
            ob, ac, atarg, tdlamret = seg["ob"], seg["ac"], seg["adv"], seg["tdlamret"]
            vpredbefore = seg["vpred"] # predicted value function before udpate
            atarg = (atarg - atarg.mean()) / atarg.std() # standardized advantage function estimate
            d = Dataset(dict(ob=ob, ac=ac, atarg=atarg, vtarg=tdlamret), shuffle=not pi.recurrent)
            # optim_batchsize = optim_batchsize or ob.shape[0]
            optim_batchsize = ob.shape[0]

            if hasattr(pi, "ob_rms"): pi.ob_rms.update(ob) # update running mean/std for policy

            assign_old_eq_new() # set old parameter values to new parameter values
            dh_logger.info(f"Rank={rank}: Optimizing...")

            # Here we do a bunch of optimization epochs over the data
            for _ in range(optim_epochs):
                losses = [] # list of tuples, each of which gives the loss for a minibatch
                for batch in d.iterate_once(optim_batchsize):
                    *newlosses, g = lossandgrad(batch["ob"], batch["ac"], batch["atarg"], batch["vtarg"], cur_lrmult)

                    t1 = time.time()
                    ## BEGIN - TIMING ##
                    adam.worker_update(g, optim_stepsize * cur_lrmult)
                    ## END - TIMING ##
                    t2 = time.time()
                    t = t2 - t1
                    dh_logger.info(jm(type='adam.worker_update', rank=rank, duration=t, start_time=t1, end_time=t2))

                    losses.append(newlosses)

            dh_logger.info(f"Rank={rank}: Evaluating losses...")
            losses = []
            for batch in d.iterate_once(optim_batchsize):
                newlosses = compute_losses(batch["ob"], batch["ac"], batch["atarg"], batch["vtarg"], cur_lrmult)
                losses.append(newlosses)
            meanlosses,_,_ = mpi_moments(losses, axis=0, use_mpi=False)

            lens = seg["ep_lens"]
            rews = seg["ep_rets"]

            episodes_so_far += len(lens)
            timesteps_so_far += sum(lens)
            iters_so_far += 1

        return pi

def flatten_lists(listoflists):
    return [el for list_ in listoflists for el in list_]
