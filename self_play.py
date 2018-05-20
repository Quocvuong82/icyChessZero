from asyncio import Future
import os
import asyncio
from asyncio.queues import Queue
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except:
    print("uvloop not detected, ignoring")
    pass
from cchess_zero import cbf
import tensorflow as tf
import numpy as np
import os
import sys
import random
import time
import argparse
from collections import deque, defaultdict, namedtuple
import scipy.stats
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from cchess_zero.gameboard import *
from net import resnet
import common
from common import board
labels = common.board.create_uci_labels()
from cchess_zero import mcts
from cchess import *
from common import board
import common
from game_convert import boardarr2netinput
uci_labels = common.board.create_uci_labels()
from cchess import BaseChessBoard
from cchess_zero import mcts_pool,mcts_async
from collections import deque, defaultdict, namedtuple
QueueItem = namedtuple("QueueItem", "feature future")
import argparse
import urllib.request
import urllib.parse
parser = argparse.ArgumentParser(description="mcts self play script") 
parser.add_argument('--verbose', '-v', action='store_true', help='verbose mode')
parser.add_argument('--gpu', '-g' , choices=[int(i) for i in list(range(8))],type=int,help="gpu core number",default=0)
parser.add_argument('--server', '-s' ,type=str,help="distributed server location",default=None)
args = parser.parse_args()

gpu_num = int(args.gpu)
server = args.server
os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_num)

class GameState():
    def __init__(self):
        self.statestr = 'RNBAKABNR/9/1C5C1/P1P1P1P1P/9/9/p1p1p1p1p/1c5c1/9/rnbakabnr'
        self.currentplayer = 'w'
        self.ys = '9876543210'[::-1]
        self.xs = 'abcdefghi'
        self.pastdic = {}
        self.maxrepeat = 0
        self.lastmove = ""
        
    def get_king_pos(self):
        board = self.statestr.replace("1", " ")
        board = board.replace("2", "  ")
        board = board.replace("3", "   ")
        board = board.replace("4", "    ")
        board = board.replace("5", "     ")
        board = board.replace("6", "      ")
        board = board.replace("7", "       ")
        board = board.replace("8", "        ")
        board = board.replace("9", "         ")
        board = board.split('/')

        for i in range(3):
            pos = board[i].find('K')
            if pos != -1:
                K = "{}{}".format(self.xs[pos],self.ys[i])
        for i in range(-1,-4,-1):
            pos = board[i].find('k')
            if pos != -1:
                k = "{}{}".format(self.xs[pos],self.ys[i])
        return K,k
            
    def game_end(self):
        #if self.statestr.find('k') == -1:
        #    return True,'w'
        #elif self.statestr.find('K') == -1:
        #    return True,'b'
        wk,bk = self.get_king_pos()
        if self.maxrepeat >= 3 and (self.lastmove[-2:] != wk and self.lastmove[-2:] != bk):
            return True,self.get_current_player()
        targetkingdic = {'b':wk,'w':bk}
        moveset = GameBoard.get_legal_moves(self.statestr,self.get_current_player())
        
        targetset = set([i[-2:] for i in moveset])
        
        targ_king = targetkingdic[self.currentplayer]
        if targ_king in targetset:
            return True,self.currentplayer
        return False,None
    
    def get_current_player(self):
        return self.currentplayer
    
    def do_move(self,move):
        self.lastmove = move
        self.statestr = GameBoard.sim_do_action(move,self.statestr)
        if self.currentplayer == 'w':
            self.currentplayer = 'b'
        elif self.currentplayer == 'b':
            self.currentplayer = 'w'
        self.pastdic.setdefault(self.statestr,0)
        self.pastdic[self.statestr] += 1
        self.maxrepeat = max(self.maxrepeat,self.pastdic[self.statestr])
    
(sess,graph),((X,training),(net_softmax,value_head)) = resnet.get_model('models/5_7_resnet_joint-two_stage/model_57',labels,GPU_CORE=[gpu_num])
queue = Queue(400)
async def push_queue( features,loop):
    future = loop.create_future()
    item = QueueItem(features, future)
    await queue.put(item)
    return future
async def prediction_worker(mcts_policy_async):
    q = queue
    while mcts_policy_async.num_proceed < mcts_policy_async._n_playout:
        if q.empty():
            await asyncio.sleep(1e-3)
            continue
        item_list = [q.get_nowait() for _ in range(q.qsize())]
        #print("processing : {} samples".format(len(item_list)))
        features = np.concatenate([item.feature for item in item_list],axis=0)
        
        action_probs, value = sess.run([net_softmax,value_head],feed_dict={X:features,training:False})
        for p, v, item in zip(action_probs, value, item_list):
            item.future.set_result((p, v))
            
async def policy_value_fn_queue(state,loop):
    bb = BaseChessBoard(state.statestr)
    statestr = bb.get_board_arr()
    net_x = np.transpose(boardarr2netinput(statestr,state.get_current_player()),[1,2,0])
    net_x = np.expand_dims(net_x,0)
    future = await push_queue(net_x,loop)
    await future
    policyout,valout = future.result()
    #policyout,valout = sess.run([net_softmax,value_head],feed_dict={X:net_x,training:False})
    #result = work.delay((state.statestr,state.get_current_player()))
    #while True:
    #    if result.ready():
    #        policyout,valout = result.get()
    #        break
    #    else:
    #        await asyncio.sleep(1e-3)
    #policyout,valout = policyout[0],valout[0][0]
    policyout,valout = policyout,valout[0]
    legal_move = GameBoard.get_legal_moves(state.statestr,state.get_current_player())
    #if state.currentplayer == 'b':
    #    legal_move = board.flipped_uci_labels(legal_move)
    legal_move = set(legal_move)
    legal_move_b = set(board.flipped_uci_labels(legal_move))
    
    action_probs = []
    if state.currentplayer == 'b':
        for move,prob in zip(uci_labels,policyout):
            if move in legal_move_b:
                move = board.flipped_uci_labels([move])[0]
                action_probs.append((move,prob))
    else:
        for move,prob in zip(uci_labels,policyout):
            if move in legal_move:
                action_probs.append((move,prob))
    #action_probs = sorted(action_probs,key=lambda x:x[1])
    return action_probs, valout

def get_random_policy(policies):
    sumnum = sum([i[1] for i in policies])
    randnum = random.random() * sumnum
    tmp = 0
    for val,pos in policies:
        tmp += pos
        if tmp > randnum:
            return val

while True:
    states = []
    moves = []

    game_states = GameState()
    mcts_policy_w = mcts_async.MCTS(policy_value_fn_queue,n_playout=400,search_threads=16
                                        ,virtual_loss=0.02,policy_loop_arg=True)
    mcts_policy_b = mcts_async.MCTS(policy_value_fn_queue,n_playout=400,search_threads=16
                                        ,virtual_loss=0.02,policy_loop_arg=True)
    result = 'peace'
    can_surrender = random.random() > 0.1
    for i in range(150):
        begin = time.time()
        is_end,winner = game_states.game_end()
        if is_end == True:
            result = winner
            break
        start = time.time()
        if i % 2 == 0:
            queue = Queue(400)
            player = 'w'

            if i < 30:
                temp = 1
            else:
                temp = 1e-2
            acts, act_probs = mcts_policy_w.get_move_probs(game_states,temp=temp,verbose=False
                ,predict_workers=[prediction_worker(mcts_policy_w)])
            policies,score = list(zip(acts, act_probs)),mcts_policy_w._root._Q
            score = -score
            if score < -0.99 and can_surrender:
                winner = 'b'
                break
        else:
            queue = Queue(400)
            player = 'b'

            if i < 30:
                temp = 1
            else:
                temp = 1e-2
            acts, act_probs = mcts_policy_b.get_move_probs(game_states,temp=temp,verbose=False
                ,predict_workers=[prediction_worker(mcts_policy_b)])
            policies,score = list(zip(acts, act_probs)),mcts_policy_b._root._Q
            if score > 0.99 and can_surrender:
                winner = 'w'
                break

        move = get_random_policy(policies)
        states.append(game_states.statestr)
        moves.append(move)
        game_states.do_move(move)
        if player == 'w':
            print('{} {} {:.4f}s {:.4f}, sel:{} pol:{} upd:{}'.format(i + 1,move,time.time() - begin,score
                ,mcts_policy_w.select_time,mcts_policy_w.policy_time,mcts_policy_w.update_time))
            mcts_policy_w.select_time,mcts_policy_w.policy_time,mcts_policy_w.update_time = 0,0,0
        else:
            print('{} {} {:.4f}s {:.4f}, sel:{} pol:{} upd:{}'.format(i + 1,move,time.time() - begin,score
                ,mcts_policy_b.select_time,mcts_policy_b.policy_time,mcts_policy_b.update_time))
            mcts_policy_b.select_time,mcts_policy_b.policy_time,mcts_policy_b.update_time = 0,0,0
        mcts_policy_w.update_with_move(move,allow_legacy=False)
        mcts_policy_b.update_with_move(move,allow_legacy=False)
        #print("move {} player {} move {} value {} time {}".format(i + 1,player,move,score,time.time() - start))
    if winner is None:
        winner = 'peace'
    cbfile = cbf.CBF(black='mcts',red='mcts',date='2018-05-113',site='北京',name='noname',datemodify='2018-05-13',
            redteam='icybee',blackteam='icybee',round='第一轮')
    cbfile.receive_moves(moves)
    stamp = time.strftime('%Y-%m-%d_%H-%M-%S',time.localtime(time.time()))
    randstamp = random.randint(0,1000)

    # send data to server if possible
    if server is not None and server[:4] == 'http':        
        print("sending gameplay to server")
        data = urllib.parse.urlencode({'name':'{}_{}_mcts-mcts_{}.cbf'.format(stamp,randstamp,winner),'content':cbfile.text})
        data = data.encode('utf-8')
        request = urllib.request.Request("{}/submit_chess".format(server))
        request.add_header("Content-Type","application/x-www-form-urlencoded;charset=utf-8")
        f = urllib.request.urlopen(request, data)
        print(f.read().decode('utf-8'))

    cbfile.dump('data/self-plays/{}_{}_mcts-mcts_{}.cbf'.format(stamp,randstamp,winner))
mcts_play_wins.append(winner)
