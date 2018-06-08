# coding: utf-8
"""
This is a PyTorch implementation based on the method
for state representation learning described in the paper "Learning State
Representations with Robotic Priors" (Jonschkowski & Brock, 2015).

This program is based on the original implementation by Rico Jonschkowski (rico.jonschkowski@tu-berlin.de):
https://github.com/tu-rbo/learning-state-representations-with-robotic-priors

"""
from __future__ import print_function, division, absolute_import

import argparse
import time
import sys
from collections import defaultdict

import numpy as np
import torch as th
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm

import plotting.representation_plot as plot_script
from models.base_learner import BaseLearner
from models import SRLConvolutionalNetwork, SRLDenseNetwork, SRLCustomCNN, TripletNet, Discriminator, \
    SRLCustomForward, SRLCustomInverse, SRLCustomForwardInverse, SRLInverseAutoEncoder

from plotting.representation_plot import plotRepresentation, plt, plotImage
from plotting.losses_plot import plotLosses
from preprocessing.data_loader import CustomDataLoader
from preprocessing.preprocess import INPUT_DIM
from preprocessing.utils import deNormalize
from utils import parseDataFolder, printRed, printYellow
from pipeline import NO_PAIRS_ERROR, NAN_ERROR
from losses.losses import autoEncoderLoss, RoboticPriorsLoss, RoboticPriorsTripletLoss, findPriorsPairs, \
    rewardModelLoss, rewardPriorLoss, forwardModelLoss, inverseModelLoss, episodePriorLoss

from models.models import encodeOneHot
# Python 2/3 compatibility
try:
    input = raw_input
except NameError:
    pass

DISPLAY_PLOTS = True
EPOCH_FLAG = 1  # Plot every 1 epoch
BATCH_SIZE = 256  #
NOISE_STD = 1e-6  # To avoid NaN (states must be different)
VALIDATION_SIZE = 0.2  # 20% of training data for validation

# Experimental: episode independent prior
BALANCED_SAMPLING = False  # Whether to do Uniform (default) or balanced sampling


class SRL4robotics(BaseLearner):
    """
    :param state_dim: (int)
    :param model_type: (str) one of "resnet", "mlp" or "custom_cnn"
    :param seed: (int)
    :param learning_rate: (float)
    :param l1_reg: (float)
    :param cuda: (bool)
    :param multi_view (bool)
    :param episode_prior (bool)
    :param reward_prior (bool)
    """

    def __init__(self, state_dim, model_type="resnet", log_folder="logs/default",
                 seed=1, learning_rate=0.001, l1_reg=0.0, cuda=False,
                 multi_view=False, no_priors=False, episode_prior=False, reward_prior=False):

        super(SRL4robotics, self).__init__(state_dim, BATCH_SIZE, seed, cuda)

        self.multi_view = multi_view
        self.episode_prior = episode_prior
        self.use_forward_loss = False
        self.use_inverse_loss = False
        self.use_reward_loss = False
        self.use_autoencoder = False
        self.reward_prior = reward_prior

        if model_type == "resnet":
            self.model = SRLConvolutionalNetwork(self.state_dim, cuda, noise_std=NOISE_STD)
        elif model_type == "custom_cnn":
            self.model = SRLCustomCNN(self.state_dim, cuda, noise_std=NOISE_STD)
        elif model_type == "triplet_cnn":
            self.model = TripletNet(self.state_dim)
        elif model_type == "mlp":
            self.model = SRLDenseNetwork(INPUT_DIM, self.state_dim, self.batch_size, cuda, noise_std=NOISE_STD)
        elif model_type == "forward_model":
            self.model = SRLCustomForward(state_dim=self.state_dim, action_dim=4, cuda=cuda)
            self.use_forward_loss = True
        elif model_type == "inverse_model":
            self.model = SRLCustomInverse(state_dim=self.state_dim, action_dim=4, cuda=cuda)
            self.use_inverse_loss = True
            self.use_reward_loss = True
        elif model_type == "fwd_inv_model":
            self.model = SRLCustomForwardInverse(state_dim=self.state_dim, cuda=cuda, ratio=1)
            self.use_forward_loss, self.use_inverse_loss = True, True
            self.use_reward_loss = False
        elif model_type == "ae_inverse":
            self.model = SRLInverseAutoEncoder(self.state_dim, action_dim=actions_dims)
            self.use_inverse_loss = True
            self.use_autoencoder = True
        else:
            raise ValueError("Unknown model: {}".format(model_type))
        print("Using {} model".format(model_type))

        if self.episode_prior:
            self.discriminator = Discriminator(2 * self.state_dim)

        if cuda:
            self.model.cuda()
            if self.episode_prior:
                self.discriminator.cuda()

        learnable_params = [param for param in self.model.parameters() if param.requires_grad]

        if self.episode_prior:
            learnable_params += [p for p in self.discriminator.parameters()]

        self.optimizer = th.optim.Adam(learnable_params, lr=learning_rate)
        self.l1_reg = l1_reg
        self.log_folder = log_folder
        self.model_type = model_type
        self.no_priors = no_priors

    def learn(self, images_path, actions, rewards, episode_starts):
        """
        Learn a state representation
        :param images_path: (numpy 1D array)
        :param actions: (numpy matrix)
        :param rewards: (numpy 1D array)
        :param episode_starts: (numpy 1D array) boolean array
                                the ith index is True if one episode starts at this frame
        :return: (numpy tensor) the learned states for the given observations
        """

        # PREPARE DATA -------------------------------------------------------------------------------------------------
        # here, we organize the data into minibatches
        # and find pairs for the respective loss terms

        num_samples = images_path.shape[0] - 1  # number of samples

        # indices for all time steps where the episode continues
        indices = np.array([i for i in range(num_samples) if not episode_starts[i + 1]], dtype='int64')
        np.random.shuffle(indices)

        # split indices into minibatches. minibatchlist is a list of lists; each
        # list is the id of the observation preserved through the training
        minibatchlist = [np.array(sorted(indices[start_idx:start_idx + self.batch_size]))
                         for start_idx in range(0, len(indices) - self.batch_size + 1, self.batch_size)]

        if len(minibatchlist[-1]) < self.batch_size:
            printYellow("Removing last minibatch of size {} < batch_size".format(len(minibatchlist[-1])))
            del minibatchlist[-1]

        # Number of minibatches used for validation:
        n_val_batches = np.round(VALIDATION_SIZE * len(minibatchlist)).astype(np.int64)
        val_indices = np.random.permutation(len(minibatchlist))[:n_val_batches]
        # Print some info
        print("{} minibatches for training, {} samples".format(len(minibatchlist) - n_val_batches,
                                                               (len(minibatchlist) - n_val_batches) * BATCH_SIZE))
        print("{} minibatches for validation, {} samples".format(n_val_batches, n_val_batches * BATCH_SIZE))
        assert n_val_batches > 0, "Not enough sample to create a validation set"

        # Stats about actions
        action_set = set(actions)
        n_actions = int(np.max(actions) + 1)
        print("{} unique actions / {} actions".format(len(action_set), n_actions))
        n_pairs_per_action = np.zeros(n_actions, dtype=np.int64)
        n_obs_per_action = np.zeros(n_actions, dtype=np.int64)

        for i in range(n_actions):
            n_obs_per_action[i] = np.sum(actions == i)

        print("Number of observations per action")
        print(n_obs_per_action)

        same_actions, dissimilar_pairs = None, None
        if not self.no_priors:
            same_actions, dissimilar_pairs = findPriorsPairs(self.batch_size, minibatchlist, actions, rewards,
                                                             n_actions, n_pairs_per_action)

        if self.episode_prior:
            idx_to_episode = {idx: episode_idx for idx, episode_idx in enumerate(np.cumsum(episode_starts))}
            minibatch_episodes = [[idx_to_episode[i] for i in minibatch] for minibatch in minibatchlist]

        data_loader = CustomDataLoader(minibatchlist, images_path,
                                       same_actions, dissimilar_pairs, cache_capacity=100,
                                       multi_view=self.multi_view,
                                       triplets=(self.model_type == "triplet_cnn"))
        # TRAINING -----------------------------------------------------------------------------------------------------
        loss_history = defaultdict(list)

        if self.model_type == "triplet_cnn":
            criterion = RoboticPriorsTripletLoss(self.model, self.l1_reg, loss_history)
        # elif self.model_type == "forward_model":
        #    pass
        else:
            criterion = RoboticPriorsLoss(self.model, self.l1_reg, loss_history)

        best_error = np.inf
        best_model_path = "{}/srl_model.pth".format(self.log_folder)
        self.model.train()
        start_time = time.time()

        for epoch in range(N_EPOCHS):
            # In each epoch, we do a full pass over the training data:
            epoch_loss, epoch_batches = 0, 0
            val_loss = 0
            pbar = tqdm(total=len(minibatchlist))
            data_loader.resetAndShuffle()

            for minibatch_num, _input in enumerate(data_loader):
                # Unpack input
                minibatch_idx, obs, next_obs, same_actions, diss_pairs = _input
                if self.cuda:
                    obs, next_obs = obs.cuda(), next_obs.cuda()
                    same_actions, diss_pairs = same_actions.cuda(), diss_pairs.cuda()

                if self.no_priors:
                    same_actions, diss_pairs = None, None

                self.optimizer.zero_grad()

                # Predict states given observations as in Time Contrastive Network (Triplet Loss) [Sermanet et al.]
                if self.model_type == "triplet_cnn":
                    states, positive_states, negative_states = self.model(obs[:, :3:, :, :], obs[:, 3:6, :, :],
                                                                          obs[:, 6:, :, :])

                    next_states, next_positive_states, next_negative_states = self.model(next_obs[:, :3:, :, :],
                                                                                         next_obs[:, 3:6, :, :],
                                                                                         next_obs[:, 6:, :, :])

                    loss = criterion(states, positive_states, negative_states, next_states, next_positive_states,
                                     diss_pairs, same_actions, no_priors=self.no_priors)
                else:
                    criterion.resetLosses()
                    if self.use_autoencoder:
                        (states, decoded_obs), (next_states, decoded_next_obs) = self.model(obs), self.model(next_obs)
                    else:
                        states, next_states = self.model(obs), self.model(next_obs)
                        decoded_obs, decoded_next_obs = None, None
                    # Actions associated to the observations of the current minibatch
                    actions_st = actions[minibatchlist[minibatch_idx]]
                    actions_st = Variable(th.from_numpy(actions_st), requires_grad=False).view(-1, 1)
                    
                    if not self.no_priors:
                        criterion.forward(states, next_states, diss_pairs, same_actions)

                    if self.cuda:
                        actions_st = actions_st.cuda()

                    if self.use_forward_loss:
                        next_states_pred = self.model.forwardModel(states, actions_st)
                        forwardModelLoss(next_states_pred, next_states, weight=2, loss_object=criterion)

                    if self.use_inverse_loss:
                        actions_pred = self.model.inverseModel(states, next_states)
                        inverseModelLoss(actions_pred, actions_st, weight=1, loss_object=criterion)

                    if self.use_reward_loss:
                        rewards_st = rewards[minibatchlist[minibatch_idx]]
                        # to set reward  between [0, 1, 2 ] for Cross-entropy (categorical reward)
                        rewards_st = Variable(th.from_numpy(rewards_st)).view(-1, 1) + 1
                        if self.cuda:
                            rewards_st = rewards_st.cuda()
                        rewards_pred = self.model.rewardModel(states, actions_st, next_states)
                        rewardModelLoss(rewards_pred, rewards_st.long(), weight=5, loss_object=criterion)
                        
                    if self.use_autoencoder:
                        autoEncoderLoss(obs, decoded_obs, next_obs, decoded_next_obs, weight=1, loss_object=criterion)

                    if self.reward_prior:
                        rewards_st = rewards[minibatchlist[minibatch_idx]]
                        rewards_st = Variable(th.from_numpy(rewards_st).float()).view(-1, 1)
                        if self.cuda:
                            rewards_st = rewards_st.cuda()
                        rewardPriorLoss(states, rewards_st, actions_st, n_actions, weight=10., loss_object=criterion)
                        
                    if self.episode_prior:
                        episodePriorLoss(minibatch_idx, minibatch_episodes, states, self.discriminator,
                                         BALANCED_SAMPLING, weight=1, loss_object=criterion, cuda=self.cuda)
                    # Compute weighted average of losses
                    criterion.updateLossHistory()
                    loss = criterion.computeTotalLoss()

                # We have to call backward in both train/val
                # to avoid memory error
                loss.backward()
                if minibatch_idx in val_indices:
                    val_loss += loss.data[0]
                    # We do not optimize on validation data
                    # so optimizer.step() is not called
                else:
                    self.optimizer.step()
                    epoch_loss += loss.data[0]
                    epoch_batches += 1
                pbar.update(1)
            pbar.close()

            train_loss = epoch_loss / float(epoch_batches)
            val_loss /= float(n_val_batches)
            # Even if loss_history is modified by RoboticPriorsLoss object
            # we make it explicit
            loss_history = criterion.loss_history
            loss_history['train_loss'].append(train_loss)
            loss_history['val_loss'].append(val_loss)
            for key in loss_history.keys():
                if key in ['train_loss', 'val_loss']:
                    continue
                loss_history[key][-1] /= epoch_batches
                if epoch + 1 < N_EPOCHS:
                    loss_history[key].append(0)

            # Save best model
            if val_loss < best_error:
                best_error = val_loss
                th.save(self.model.state_dict(), best_model_path)

            if np.isnan(train_loss):
                print("NaN Loss, consider increasing NOISE_STD in the gaussian noise layer")
                sys.exit(NAN_ERROR)

            # Then we print the results for this epoch:
            if (epoch + 1) % EPOCH_FLAG == 0:
                print("Epoch {:3}/{}, train_loss:{:.4f} val_loss:{:.4f}".format(epoch + 1, N_EPOCHS, train_loss,
                                                                                val_loss))
                print("{:.2f}s/epoch".format((time.time() - start_time) / (epoch + 1)))
                if DISPLAY_PLOTS:
                    # Optionally plot the current state space
                    plotRepresentation(self.predStatesWithDataLoader(data_loader, restore_train=True), rewards,
                                       add_colorbar=epoch == 0,
                                       name="Learned State Representation (Training Data)")
                    # plt.clf()
                    # plt.pause(0.001)
                    # plotLosses(loss_history, args.log_folder)

                    if self.use_autoencoder:
                        # Plot Reconstructed Image
                        plotImage(deNormalize(obs[0].data.cpu().numpy()), "Input Image (Train)")
                        plotImage(deNormalize(decoded_obs[0].data.cpu().numpy()), "Reconstructed Image")
        if DISPLAY_PLOTS:
            plt.close("Learned State Representation (Training Data)")

        # Load best model before predicting states
        self.model.load_state_dict(th.load(best_model_path))

        print("Predicting states for all the observations...")
        # return predicted states for training observations
        return loss_history, self.predStatesWithDataLoader(data_loader, restore_train=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch SRL with robotic priors')
    parser.add_argument('--epochs', type=int, default=50, metavar='N',
                        help='number of epochs to train (default: 50)')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--state-dim', type=int, default=2, help='state dimension (default: 2)')
    parser.add_argument('-bs', '--batch-size', type=int, default=256, help='batch_size (default: 256)')
    parser.add_argument('--val-size', type=float, default=0.2, help='Validation set size in percentage (default: 0.2)')
    parser.add_argument('--training-set-size', type=int, default=-1,
                        help='Limit size (number of samples) of the training set (default: -1)')
    parser.add_argument('-lr', '--learning-rate', type=float, default=0.005, help='learning rate (default: 0.005)')
    parser.add_argument('--l1-reg', type=float, default=0.0, help='L1 regularization coeff (default: 0.0)')
    parser.add_argument('--no-cuda', action='store_true', default=False, help='disables CUDA training')
    parser.add_argument('--no-plots', action='store_true', default=False, help='disables plots')
    parser.add_argument('--model-type', type=str, default="custom_cnn",
                        choices=['custom_cnn', 'resnet', 'mlp', 'triplet_cnn', 'forward_model', 'inverse_model',
                                 'fwd_inv_model', 'ae_inverse'],
                        help='Model architecture (default: "custom_cnn")')
    parser.add_argument('--data-folder', type=str, default="", help='Dataset folder', required=True)
    parser.add_argument('--log-folder', type=str, default='logs/default_folder',
                        help='Folder within logs/ where the experiment model and plots will be saved')
    parser.add_argument('--multi-view', action='store_true', default=False,
                        help='Enable use of multiple camera')
    parser.add_argument('--reward-prior', action='store_true', default=False,
                        help='Enable reward prior (enforce correlation between states/actions and reward)')
    parser.add_argument('--episode-prior', action='store_true', default=False,
                        help='Enable episode independent prior')
    parser.add_argument('--balanced-sampling', action='store_true', default=False,
                        help='Force balanced sampling for episode independent prior instead of uniform')
    parser.add_argument('--no-priors', action='store_true', default=False,
                        help='Disable use of priors - in case of triplet loss')

    args = parser.parse_args()
    args.cuda = not args.no_cuda and th.cuda.is_available()
    args.data_folder = parseDataFolder(args.data_folder)
    DISPLAY_PLOTS = not args.no_plots
    N_EPOCHS = args.epochs
    BATCH_SIZE = args.batch_size
    VALIDATION_SIZE = args.val_size
    BALANCED_SAMPLING = args.balanced_sampling
    plot_script.INTERACTIVE_PLOT = DISPLAY_PLOTS

    print('Log folder: {}'.format(args.log_folder))

    print('Loading data ... ')
    training_data = np.load("data/{}/preprocessed_data.npz".format(args.data_folder))
    actions = training_data['actions']
    rewards, episode_starts = training_data['rewards'], training_data['episode_starts']

    ground_truth = np.load("data/{}/ground_truth.npz".format(args.data_folder))
    # Try to convert old python 2 format
    try:
        images_path = np.array([path.decode("utf-8") for path in ground_truth['images_path']])
    except AttributeError:
        images_path = ground_truth['images_path']

    print('Learning a state representation ... ')
    srl = SRL4robotics(args.state_dim, model_type=args.model_type, seed=args.seed,
                       log_folder=args.log_folder, learning_rate=args.learning_rate,
                       l1_reg=args.l1_reg, cuda=args.cuda, multi_view=args.multi_view,
                       no_priors=args.no_priors, episode_prior=args.episode_prior, reward_prior=args.reward_prior)

    if args.training_set_size > 0:
        limit = args.training_set_size
        actions = actions[:limit]
        images_path = images_path[:limit]
        rewards = rewards[:limit]
        episode_starts = episode_starts[:limit]

    loss_history, learned_states = srl.learn(images_path, actions, rewards, episode_starts)
    # Save losses losses history
    np.savez('{}/loss_history.npz'.format(args.log_folder), **loss_history)
    # Save plot
    plotLosses(loss_history, args.log_folder)

    srl.saveStates(learned_states, images_path, rewards, args.log_folder)

    name = "Learned State Representation\n {}".format(args.log_folder.split('/')[-1])
    path = "{}/learned_states.png".format(args.log_folder)
    plotRepresentation(learned_states, rewards, name, add_colorbar=True, path=path)

    # Do not close plot at the end of training
    if DISPLAY_PLOTS:
        input('\nPress any key to exit.')
