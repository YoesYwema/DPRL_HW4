"""
Code for general deep Q-learning using Keras that can take as inputs scalars, vectors and matrices

.. Author: Vincent Francois-Lavet
"""

import numpy as np
np.set_printoptions(threshold=np.nan)
from keras.optimizers import SGD,RMSprop
from keras import backend as K
from ..base_classes import QNetwork
from .NN_keras_lp import NN # Default Neural network used

def mean_squared_error(y_true, y_pred):
    return K.mean(K.square(y_pred - y_true), axis=-1) # tend to reduce the square of the diff between y_pred and y_true

def exp_dec_error(y_true, y_pred):
    #return - K.sum(  K.sqrt( K.clip(y_pred,0.000001,1))  , axis=-1, keepdims=True ) # tend to increase y_pred
    return K.exp( - 2*K.sqrt( K.sum(K.square(y_pred), axis=-1, keepdims=True) + 0.0001 )  ) # tend to increase y_pred

def rms_from_squared_components(y_true, y_pred):
    return - K.sum(  K.sqrt( K.clip(y_pred,0.000001,1))  , axis=-1, keepdims=True ) # tend to increase y_pred --> loss -1

def squared_error_from_squared_components(y_true, y_pred):
    return - K.sum(  K.clip(y_pred,0.,1)  , axis=-1, keepdims=True ) # tend to increase y_pred --> loss -1

def loss_diff_s_s_(y_true, y_pred):
    return K.square(   1.    -    K.sqrt(  K.clip( K.sum(y_pred,axis=-1,keepdims=True), 0.000001 , 1. )  )     ) # tend to increase y_pred --> loss -1

class MyQNetwork(QNetwork):
    """
    Deep Q-learning network using Keras (with any backend)
    
    Parameters
    -----------
    environment : object from class Environment
    rho : float
        Parameter for rmsprop. Default : 0.9
    rms_epsilon : float
        Parameter for rmsprop. Default : 0.0001
    momentum : float
        Default : 0
    clip_delta : float
        Not implemented.
    freeze_interval : int
        Period during which the target network is freezed and after which the target network is updated. Default : 1000
    batch_size : int
        Number of tuples taken into account for each iteration of gradient descent. Default : 32
    update_rule: str
        {sgd,rmsprop}. Default : rmsprop
    random_state : numpy random number generator
    double_Q : bool, optional
        Activate or not the double_Q learning.
        More informations in : Hado van Hasselt et al. (2015) - Deep Reinforcement Learning with Double Q-learning.
    neural_network : object, optional
        default is deer.qnetworks.NN_keras
    """

    def __init__(self, environment, rho=0.9, rms_epsilon=0.0001, momentum=0, clip_delta=0, freeze_interval=1000, batch_size=32, update_rule="rmsprop", random_state=np.random.RandomState(), double_Q=False, neural_network=NN):
        """ Initialize environment
        
        """
        QNetwork.__init__(self,environment, batch_size)

        
        self._rho = rho
        self._rms_epsilon = rms_epsilon
        self._momentum = momentum
        self._update_rule = update_rule
        self._freeze_interval = freeze_interval
        self._double_Q = double_Q
        self._random_state = random_state
        self.update_counter = 0    
        self.loss_T=0
        self.loss_T2=0
        self.loss_disentangle_t=0
        self.loss_disentangle_a=0
        self.lossR=0
        self.loss_disambiguate1=0
        self.loss_disambiguate2=0

        
        self.learn_and_plan = neural_network(self._batch_size, self._input_dimensions, self._n_actions, self._random_state)

        self.encoder = self.learn_and_plan.encoder_model()
        self.encoder_diff = self.learn_and_plan.encoder_diff_model(self.encoder)
        
        self.Q = self.learn_and_plan.Q_model()
        self.R = self.learn_and_plan.R_model()
        self.transition = self.learn_and_plan.transition_model()
        self.transition2 = self.learn_and_plan.transition_model2()

        self.full_Q = self.learn_and_plan.full_Q_model(self.encoder,self.Q)
        
        # used to fit rewards
        self.full_R = self.learn_and_plan.full_R_model(self.encoder,self.R)
        
        # used to fit transitions
        self.diff_Tx_x_ = self.learn_and_plan.diff_Tx_x_(self.encoder,self.transition)#full_transition_model(self.encoder,self.transition)
        
        
        # constraint on consecutive t
        self.diff_s_s_ = self.learn_and_plan.diff_s_s_(self.encoder)
#        self.diff_Tx = self.learn_and_plan.diff_Tx(self.transition)

        # used to disentangle actions
        #self.diff_sa_sa = self.learn_and_plan.diff_sa_sa(self.encoder,self.transition)
                
        layers=self.full_Q.layers
        # Grab all the parameters together.
        self.params = [ param
                    for layer in layers 
                    for param in layer.trainable_weights ]

        self._compile()

        self.next_full_Q = self.learn_and_plan.full_Q_model(self.encoder,self.Q)
        self.next_full_Q.compile(optimizer='rmsprop', loss='mse') #The parameters do not matter since training is done on self.full_Q

        layers=self.next_full_Q.layers
        # Grab all the parameters together.
        self.next_params = [ param
                    for layer in layers 
                    for param in layer.trainable_weights ]

        self._resetQHat()

    def getAllParams(self):
        params_value=[]
        for i,p in enumerate(self.params):
            params_value.append(K.get_value(p))
        return params_value

    def setAllParams(self, list_of_values):
        for i,p in enumerate(self.params):
            K.set_value(p,list_of_values[i])

    def train(self, states_val, actions_val, rewards_val, next_states_val, terminals_val):
        """
        Train one batch.

        1. Set shared variable in states_shared, next_states_shared, actions_shared, rewards_shared, terminals_shared         
        2. perform batch training

        Parameters
        -----------
        states_val : list of batch_size * [list of max_num_elements* [list of k * [element 2D,1D or scalar]])
        actions_val : b x 1 numpy array of integers
        rewards_val : b x 1 numpy array
        next_states_val : list of batch_size * [list of max_num_elements* [list of k * [element 2D,1D or scalar]])
        terminals_val : b x 1 numpy boolean array

        Returns
        -------
        Average loss of the batch training (RMSE)
        Individual (square) losses for each tuple
        """
        
        onehot_actions = np.zeros((self._batch_size, self._n_actions))
        onehot_actions[np.arange(self._batch_size), actions_val[:,0]] = 1
        #onehot_actions_rand = np.zeros((self._batch_size, self._n_actions))
        #onehot_actions_rand[np.arange(self._batch_size), np.random.randint(0,2,(32))] = 1
        Es_=self.encoder.predict([next_states_val[0]])
        Es=self.encoder.predict([states_val[0]])
        ETs=self.transition.predict([Es,onehot_actions])
                   
        if(self.update_counter%100==0):
            print states_val[0][0]
            print "len(states_val)"
            print len(states_val)
            print next_states_val[0][0]
            print actions_val, rewards_val, terminals_val
            print "Es"
            print Es
            print "ETs,Es_"
            print ETs,Es_
            
        # Fit transition
        self.loss_T+=self.diff_Tx_x_.train_on_batch([states_val[0],next_states_val[0],onehot_actions], np.zeros((32,3)))

        # Fit rewards
        self.lossR+=self.full_R.train_on_batch([states_val[0],onehot_actions], rewards_val) 

        # Loss to ensure entropy but limited volume in abstract state space, avg=0 and sigma=1
        # reduce the squared value of the abstract features
        self.loss_disambiguate1+=self.encoder.train_on_batch([states_val[0]],np.zeros((32,3)))
#        # Increase the entropy in the abstract features of two states
        self.loss_disambiguate2+=self.encoder_diff.train_on_batch([states_val[0],np.roll(states_val[0],1,axis=0)],np.zeros((32,3)))

        #print self.loss_disambiguate1
        #self.loss_disentangle_t+=self.diff_s_s_.train_on_batch([states_val[0],next_states_val[0]], np.ones(32)) #np.ones((32,3))*2) 

        # Disentangle actions
        #self.loss_disentangle_a+=self.diff_sa_sa.train_on_batch([states_val[0],onehot_actions,onehot_actions_rand], np.ones(32))

#
#        # Loss to have all s' following s,a with a to a distance 1 of s,a)
#        tiled_x=np.tile(Es,(self._n_actions,1))
#        tiled_onehot_actions=np.tile(onehot_actions,(self._n_actions,1))
#        tiled_onehot_actions2=np.repeat(np.diag(np.ones(self._n_actions)),self._batch_size,axis=0)
#        #self.loss_disentangle_a+=self.diff_Tx.train_on_batch([tiled_x,tiled_onehot_actions,tiled_x,tiled_onehot_actions2], np.ones(32*self._n_actions)) 


        
        if(self.update_counter%100==0):
#            print "self.transition"
#            print [ K.get_value(param)
#                    for layer in self.transition.layers
#                    for param in layer.trainable_weights ]
#            print self.transition.layers
#            print "losses"
            print "self.loss_T/100.,self.loss_T2/1000.,self.lossR/100.,self.loss_disentangle_t/100.,self.loss_disambiguate1/100.,self.loss_disambiguate2/100."
            print self.loss_T/100.,self.loss_T2/1000.,self.lossR/100.,self.loss_disentangle_t/100.,self.loss_disambiguate1/100.,self.loss_disambiguate2/100.
            self.loss_T=0
            self.loss_T2=0
            self.lossR=0

            self.loss_disentangle_t=0
            self.loss_disentangle_a=0
            
            self.loss_disambiguate1=0
            self.loss_disambiguate2=0



        if self.update_counter % self._freeze_interval == 0:
            self._resetQHat()
        
        next_q_vals = self.next_full_Q.predict([next_states_val[0],np.zeros((32,self.learn_and_plan.internal_dim))])
        
        if(self._double_Q==True):
            next_q_vals_current_qnet=self.full_Q.predict(next_states_val.tolist())
            argmax_next_q_vals=np.argmax(next_q_vals_current_qnet, axis=1)
            max_next_q_vals=next_q_vals[np.arange(self._batch_size),argmax_next_q_vals].reshape((-1, 1))
        else:
            max_next_q_vals=np.max(next_q_vals, axis=1, keepdims=True)

        not_terminals=np.ones_like(terminals_val) - terminals_val
        
        target = rewards_val + not_terminals * self._df * max_next_q_vals.reshape((-1))
        
        q_vals=self.full_Q.predict([states_val[0],np.zeros((32,self.learn_and_plan.internal_dim))])

        # In order to obtain the individual losses, we predict the current Q_vals and calculate the diff
        q_val=q_vals[np.arange(self._batch_size), actions_val.reshape((-1,))]#.reshape((-1, 1))        
        diff = - q_val + target 
        loss_ind=pow(diff,2)
                
        q_vals[  np.arange(self._batch_size), actions_val.reshape((-1,))  ] = target
                
        # Is it possible to use something more flexible than this? 
        # Only some elements of next_q_vals are actual value that I target. 
        # My loss should only take these into account.
        # Workaround here is that many values are already "exact" in this update
        #if (self.update_counter<10000):
        noise_to_be_robust=np.random.normal(size=(32,self.learn_and_plan.internal_dim))*0.#25

        loss=self.full_Q.train_on_batch([states_val[0],noise_to_be_robust] , q_vals ) 
        #print "self.q_vals.optimizer.lr"
        #print K.eval(self.q_vals.optimizer.lr)
        
        if(self.update_counter%100==0):
            print self.update_counter
        
        self.update_counter += 1        

        # loss*self._n_actions = np.average(loss_ind)
        return np.sqrt(loss),loss_ind


#    def train_model(self, states_val, actions_val, rewards_val, next_states_val, terminals_val):
#        """
#        Train the model based part
#
#        1. Set shared variable in states_shared, next_states_shared, actions_shared, rewards_shared, terminals_shared         
#        2. perform batch training
#
#        Parameters
#        -----------
#        states_val : list of batch_size * [list of max_num_elements* [list of k * [element 2D,1D or scalar]])
#        actions_val : b x 1 numpy array of integers
#        rewards_val : b x 1 numpy array
#        next_states_val : list of batch_size * [list of max_num_elements* [list of k * [element 2D,1D or scalar]])
#        terminals_val : b x 1 numpy boolean array
#
#        Returns
#        -------
#        Average loss of the batch training (RMSE)
#        Individual (square) losses for each tuple
#        """
#
#        onehot_actions = np.zeros((self._batch_size, self._n_actions))
#        onehot_actions[np.arange(self._batch_size), actions_val[:,0]] = 1
#        Es_=self.encoder.predict([next_states_val[0]])
#        Es=self.encoder.predict([states_val[0]])
#        ETs=self.transition.predict([Es,onehot_actions])
#
##        if(self.update_counter>3000):
#        self.loss_T2=self.transition2.train_on_batch([Es,onehot_actions], Es_)
##        if(self.update_counter%100==0):
##            loss=0.
##            for i in range (100):
##                loss+=self.transition2.train_on_batch([Es,onehot_actions], Es_)
##                if(i%10==0):
##                    print "loss/(i+1)"
##                    print loss/(i+1)
##            print "loss/100."
##            print loss/100.
#            #print K.get_value(self.transition2.optimizer.lr)
#            #print [ K.get_value(param)
#            #        for layer in self.encoder.layers
#            #        for param in layer.trainable_weights ][0][0]
#        return self.loss_T2



    def qValues(self, state_val):
        """ Get the q values for one belief state (without planning)

        Arguments
        ---------
        state_val : one belief state

        Returns
        -------
        The q values for the provided belief state
        """ 
        return self.full_Q.predict([np.expand_dims(state,axis=0) for state in state_val]+[np.zeros((32,self.learn_and_plan.internal_dim))])[0]

    def qValues_planning(self, state_val, d=2.):
        """ Get the q values for one belief state with a planning depth d

        Arguments
        ---------
        state_val : one belief state
        d : planning depth

        Returns
        -------
        The q values with planning depth d for the provided belief state
        """ 
        identity_matrix = np.diag(np.ones(self._n_actions))
        
        encoded_x = self.encoder.predict([np.expand_dims(state,axis=0) for state in state_val])

        q_vals_d0=self.Q.predict([encoded_x])[0]
        #print "q_vals_d0"
        #print q_vals_d0
        #tile3_encoded_x=np.array([enc for enc in encoded_x for i in range(self._n_actions)])
        tile3_encoded_x=np.tile(encoded_x,(self._n_actions,1))
        #print tile3_encoded_x
        r_vals_d0=np.array(self.R.predict([tile3_encoded_x,identity_matrix])).reshape((self._n_actions))
        
        #tile3_state_val=np.array([state for state in state_val for i in range(self._n_actions)])
        #tile3_state_val=np.tile(state_val,(3,1,1,1))
        
        next_x_predicted=self.transition2.predict([tile3_encoded_x,identity_matrix])
        q_vals_d1=self.Q.predict([next_x_predicted])
        #print q_vals_d1
        #print [np.max(vals) for vals in q_vals_d1]
        #print r_vals_d0
        #print (1-1/d)+(1-1/d)**2
        #print "r_vals_d0+self._df*np.array([np.max(vals) for vals in q_vals_d1])"
        #print r_vals_d0+self._df*np.array([np.max(vals) for vals in q_vals_d1])
        #print ((1-1/d)+(1-1/d)**2)*np.array(q_vals_d0)+((1-1/d)**2)*np.array([np.max(vals) for vals in q_vals_d1])
        return ((1-1/d)+(1-1/d)**2)*np.array(q_vals_d0)+((1-1/d)**2)*(r_vals_d0+self._df*np.array([np.max(vals) for vals in q_vals_d1]))

    def chooseBestAction(self, state):
        """ Get the best action for a belief state

        Arguments
        ---------
        state : one belief state

        Returns
        -------
        The best action : int
        """        
        q_vals = self.qValues_planning(state)#self.qValues(state)#
        return np.argmax(q_vals),np.max(q_vals)
        
    def _compile(self):
        """ compile self.q_vals
        """
        if (self._update_rule=="sgd"):
            optimizer = SGD(lr=self._lr, momentum=self._momentum, nesterov=False)
        elif (self._update_rule=="rmsprop"):
            optimizer = RMSprop(lr=self._lr, rho=self._rho, epsilon=self._rms_epsilon)
        else:
            raise Exception('The update_rule '+self._update_rule+' is not implemented.')
        
        self.full_Q.compile(optimizer=optimizer, loss='mse')

        optimizer1=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06) # Different optimizers for each network; otherwise not possible to modify each
        optimizer2=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06) # separately (e.g. lr)
        optimizer3=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06)
        optimizer4=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06)
        optimizer5=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06)
        optimizer6=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06)
        optimizer7=RMSprop(lr=self._lr, rho=0.9, epsilon=1e-06)

        self.diff_Tx_x_.compile(optimizer=optimizer1, loss='mse') # Fit transitions
        self.transition2.compile(optimizer=optimizer2, loss='mse') # Fit accurate transitions without encoders
        self.full_R.compile(optimizer=optimizer3, loss='mse') # Fit rewards

        self.encoder.compile(optimizer=optimizer4,
                  loss=mean_squared_error)
        self.encoder_diff.compile(optimizer=optimizer5,
                  loss=exp_dec_error)
                  #metrics=['accuracy'])

        self.diff_s_s_.compile(optimizer=optimizer6,
                  loss=loss_diff_s_s_)#exp_dec_error)#'mse')
                  #metrics=['accuracy'])

#        self.diff_sa_sa.compile(optimizer=optimizer7,
#                  loss=loss_diff_s_s_)

#        self.diff_Tx.compile(optimizer=optimizer,
#                  loss=mean_squared_error)
#                  #metrics=['accuracy'])

    def _resetQHat(self):
        for i,(param,next_param) in enumerate(zip(self.params, self.next_params)):
            K.set_value(next_param,K.get_value(param))

    def setLearningRate(self, lr):
        """ Setting the learning rate

        Parameters
        -----------
        lr : float
            The learning rate that has to bet set
        """
        self._lr = lr
        print "modif lr"
        # Changing the learning rates (NB:recompiling seems to lead to memory leaks!)
        K.set_value(self.full_Q.optimizer.lr, self._lr)

        K.set_value(self.full_R.optimizer.lr, self._lr/10.)
        K.set_value(self.diff_Tx_x_.optimizer.lr, self._lr/10.)
        
        K.set_value(self.transition2.optimizer.lr, self._lr/10.)

        K.set_value(self.encoder.optimizer.lr, self._lr/100.)
        K.set_value(self.encoder_diff.optimizer.lr, self._lr/10.)

        K.set_value(self.diff_s_s_.optimizer.lr, self._lr/10.)
#        K.set_value(self.diff_sa_sa.optimizer.lr, self._lr/10.)
#        K.set_value(self.diff_Tx.optimizer.lr, self._lr/10.)
