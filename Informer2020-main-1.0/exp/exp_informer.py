from data.data_loader import Dataset_Custom, Dataset_Pred
from exp.exp_basic import Exp_Basic
from models.model import Informer, InformerStack

from utils.tools import EarlyStopping, adjust_learning_rate
from utils.metrics import metric

import numpy as np

import torch
import torch.nn as nn
from torch import optim
from torch.utils.data import DataLoader

import os
import time

import pandas as pd

import warnings
warnings.filterwarnings('ignore')

import datetime
class Exp_Informer(Exp_Basic):
    def __init__(self, args):
        super(Exp_Informer, self).__init__(args)
    
    def _build_model(self):
        model_dict = {
            'informer':Informer,
            'informerstack':InformerStack,
        }
        if self.args.model=='informer' or self.args.model=='informerstack':
            e_layers = self.args.e_layers if self.args.model=='informer' else self.args.s_layers
            model = model_dict[self.args.model](
                self.args.is_img_embed,
                self.args.img_channel,
                self.args.enc_in,
                self.args.dec_in, 
                self.args.c_out, 
                self.args.seq_len,
                self.args.label_len,
                self.args.pred_len,
                self.args.factor,
                self.args.d_model, 
                self.args.n_heads, 
                e_layers, # self.args.e_layers,
                self.args.d_layers, 
                self.args.d_ff,
                self.args.dropout, 
                self.args.attn,
                self.args.embed,
                self.args.freq,
                self.args.activation,
                self.args.output_attention,
                self.args.distil,
                self.args.mix,
                self.device
            ).float()
        
        if self.args.use_multi_gpu and self.args.use_gpu:
            model = nn.DataParallel(model, device_ids=self.args.device_ids)
        return model

    def _get_data(self, flag):
        args = self.args

        data_dict = {
            'custom':Dataset_Custom,
        }
        Data = data_dict[self.args.data]
        timeenc = 0 if args.embed!='timeF' else 1

        if flag == 'test':
            shuffle_flag = False; drop_last = True; batch_size = args.batch_size; freq=args.freq
        elif flag=='pred':
            shuffle_flag = False; drop_last = False; batch_size = 1; freq=args.detail_freq
            Data = Dataset_Pred
        else:
            shuffle_flag = True; drop_last = True; batch_size = args.batch_size; freq=args.freq
        data_set = Data(
            valid_data_path = args.valid_data_path,
            data_division = args.data_division,
            lag_step = args.lag_step,
            is_img_embed=args.is_img_embed,
            img_channel = args.img_channel,
            root_img_path=args.root_img_path,
            img_path=args.img_path,
            img_pred_len = args.img_pred_len,
            root_path=args.root_path,
            data_path=args.data_path,
            flag=flag,
            size=[args.seq_len, args.label_len, args.pred_len],
            features=args.features,
            target=args.target,
            inverse=args.inverse,
            timeenc=timeenc,
            freq=freq,
            cols=args.cols
        )
        print(flag, len(data_set))
        data_loader = DataLoader(
            data_set,
            batch_size=batch_size,
            shuffle=shuffle_flag,
            num_workers=args.num_workers,
            drop_last=drop_last)

        return data_set, data_loader

    def _select_optimizer(self):
        model_optim = optim.Adam(self.model.parameters(), lr=self.args.learning_rate)
        return model_optim
    
    def _select_criterion(self):
        criterion =  nn.MSELoss()
        return criterion

    def vali(self, vali_data, vali_loader, criterion):
        self.model.eval()
        total_loss = []
        for i, (batch_x,batch_y,batch_x_mark,batch_y_mark, img_data) in enumerate(vali_loader):
            pred, true = self._process_one_batch(self.args.is_img_embed, self.args.img_channel,
                vali_data, batch_x, batch_y, batch_x_mark, batch_y_mark, img_data)
            loss = criterion(pred.detach().cpu(), true.detach().cpu())
            total_loss.append(loss)
        total_loss = np.average(total_loss)
        self.model.train()
        return total_loss

    def train(self, setting):
        train_data, train_loader = self._get_data(flag = 'train')
        vali_data, vali_loader = self._get_data(flag = 'val')
        test_data, test_loader = self._get_data(flag = 'test')

        path = os.path.join(self.args.checkpoints, setting)
        if not os.path.exists(path):
            os.makedirs(path)

        time_now = time.time()
        
        train_steps = len(train_loader)
        early_stopping = EarlyStopping(patience=self.args.patience, verbose=True)
        
        model_optim = self._select_optimizer()
        criterion =  self._select_criterion()

        if self.args.use_amp:
            scaler = torch.cuda.amp.GradScaler()

        for epoch in range(self.args.train_epochs):
            iter_count = 0
            train_loss = []
            
            self.model.train()
            epoch_time = time.time()
            for i, (batch_x,batch_y,batch_x_mark,batch_y_mark, img_data) in enumerate(train_loader):
                iter_count += 1
                model_optim.zero_grad()
                pred, true = self._process_one_batch(self.args.is_img_embed, self.args.img_channel,
                    train_data, batch_x, batch_y, batch_x_mark, batch_y_mark, img_data)
                loss = criterion(pred, true)
                train_loss.append(loss.item())
                
                if (i+1) % 100==0:
                    print("\titers: {0}, epoch: {1} | loss: {2:.7f}".format(i + 1, epoch + 1, loss.item()))
                    speed = (time.time()-time_now)/iter_count
                    left_time = speed*((self.args.train_epochs - epoch)*train_steps - i)
                    print('\tspeed: {:.4f}s/iter; left time: {:.4f}s'.format(speed, left_time))
                    iter_count = 0
                    time_now = time.time()
                
                if self.args.use_amp:
                    scaler.scale(loss).backward()
                    scaler.step(model_optim)
                    scaler.update()
                else:
                    loss.backward()
                    model_optim.step()

            print("Epoch: {} cost time: {}".format(epoch+1, time.time()-epoch_time))
            train_loss = np.average(train_loss)
            vali_loss = self.vali(vali_data, vali_loader, criterion)
            test_loss = self.vali(test_data, test_loader, criterion)

            print("Epoch: {0}, Steps: {1} | Train Loss: {2:.7f} Vali Loss: {3:.7f} Test Loss: {4:.7f}".format(
                epoch + 1, train_steps, train_loss, vali_loss, test_loss))
            early_stopping(vali_loss, self.model, path)
            if early_stopping.early_stop:
                print("Early stopping")
                break

            adjust_learning_rate(model_optim, epoch+1, self.args)
            
        best_model_path = path+'/'+'checkpoint.pth'
        self.model.load_state_dict(torch.load(best_model_path))
        
        return self.model

    def test(self, setting, args):
        test_data, test_loader = self._get_data(flag='test')
        
        self.model.eval()
        
        preds = []
        trues = []
        
        for i, (batch_x,batch_y,batch_x_mark,batch_y_mark,img_data) in enumerate(test_loader):
            pred, true = self._process_one_batch(self.args.is_img_embed,self.args.img_channel,
                test_data, batch_x, batch_y, batch_x_mark, batch_y_mark, img_data)
            preds.append(pred.detach().cpu().numpy())
            trues.append(true.detach().cpu().numpy())

        preds = np.array(preds)
        trues = np.array(trues)
        preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
        trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])
        print('test shape:', preds.shape, trues.shape)

        # result save
        folder_path = './results/' + setting +'/'
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        mae, mse, rmse, mape, mspe = metric(preds, trues)
        print('mse:{}, mae:{}, rmse:{}'.format(mse, mae, rmse))

        np.save(folder_path+'metrics.npy', np.array([mae, mse, rmse, mape, mspe]))
        np.save(folder_path+'pred.npy', preds)
        np.save(folder_path+'true.npy', trues)

        folder_path1 = './results/'
        f = open(folder_path1+'tem_result.txt',mode='a')
        result = 'ti{}_dt{}_ie{}_dm_{}_lr{}_bs{}_sl{}_ll{}_pl{}_ipl{}_lg{}_mse:{}, mae:{}, rmse:{}'.format(datetime.datetime.now(), args.img_path,args.is_img_embed,args.d_model,args.learning_rate, args.batch_size,args.seq_len, args.label_len, args.pred_len,args.img_pred_len, args.lag_step,mse, mae, rmse)
        f.write(result)
        f.write('\n')
        f.close()
        return

    def predict(self, setting, args, load=True):
        pred_data, pred_loader = self._get_data(flag='pred')
        if load:
            path = os.path.join(self.args.checkpoints, setting)
            best_model_path = path+'/'+'checkpoint.pth'
            self.model.load_state_dict(torch.load(best_model_path))

        self.model.eval()
        
        preds = []
        
        for i, (batch_x,batch_y,batch_x_mark,batch_y_mark,img_data) in enumerate(pred_loader):
            pred, true = self._process_one_batch(self.args.is_img_embed,self.args.img_channel,
                pred_data, batch_x, batch_y, batch_x_mark, batch_y_mark,img_data)
            preds.append(pred.detach().cpu().numpy())

        preds = np.array(preds)
        preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
        
        # result save
        folder_path = './results/' + setting +'/'
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
        
        np.save(folder_path+'real_prediction.npy', preds)

        preds = preds.squeeze()
        preds = preds[::8]
        preds = preds.reshape(preds.shape[0] * preds.shape[1], 1).squeeze()
        scale = 32
        trues = pd.read_csv(os.path.join(self.args.root_path,
                                          self.args.valid_data_path))['tem'].values
        trues = trues[scale:]
        print('trues',trues.shape)
        print('preds', preds.shape)
        mae = np.mean(np.abs(trues[:] - preds[:]))
        mse = np.mean((trues[:] - preds[:]) ** 2)


        folder_path1 = './results/'
        f = open(folder_path1 + 'tem_result.txt', mode='a')
        result = 't{}_{}_dt{}_ie{}_dm_{}_lr{}_bs{}_sl{}_ll{}_pl{}_ipl{}_lg{}_mse:{}, mae:{}'.format(
            datetime.datetime.now(), 'pred', args.img_path, args.is_img_embed, args.d_model, args.learning_rate,
            args.batch_size, args.seq_len, args.label_len, args.pred_len, args.img_pred_len, args.lag_step, mse, mae)
        print(result)
        f.write(result)
        f.write('\n')
        f.close()
        return

    def _process_one_batch(self,is_img_embed, img_channel,dataset_object, batch_x, batch_y, batch_x_mark, batch_y_mark, img_data):
        batch_x = batch_x.float().to(self.device)
        if is_img_embed:
            img_data = img_data.float()
        batch_y = batch_y.float()

        batch_x_mark = batch_x_mark.float().to(self.device)
        batch_y_mark = batch_y_mark.float().to(self.device)

        # decoder input
        if self.args.padding==0:
            # dec_inp is 0 placeholder
            dec_inp = torch.zeros([batch_y.shape[0], self.args.pred_len, batch_y.shape[-1]]).float()
            if is_img_embed:
                img_zero = torch.zeros(
                    [batch_y.shape[0], self.args.pred_len-self.args.img_pred_len, img_channel, img_data.shape[-2], img_data.shape[-1]])
                img_data = torch.cat([img_data, img_zero], dim=1).float().to(self.device)

        elif self.args.padding==1:
            dec_inp = torch.ones([batch_y.shape[0], self.args.pred_len, batch_y.shape[-1]]).float()
            if is_img_embed:
                img_one = torch.ones(
                    [batch_y.shape[0], self.args.pred_len-self.args.img_pred_len, img_channel, img_data.shape[-2], img_data.shape[-1]])
                img_data = torch.cat([img_data, img_one], dim=1).float().to(self.device)

        dec_inp = torch.cat([batch_y[:,:self.args.label_len,:], dec_inp], dim=1).float().to(self.device)
        # encoder - decoder
        if self.args.use_amp:
            with torch.cuda.amp.autocast():
                if self.args.output_attention:
                    outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark, img_data)[0]
                else:
                    outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark, img_data)
        else:
            if self.args.output_attention:
                outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark, img_data)[0]
            else:
                outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark, img_data)
        if self.args.inverse:
            outputs = dataset_object.inverse_transform(outputs)
        f_dim = -1 if self.args.features=='MS' else 0
        batch_y = batch_y[:,-self.args.pred_len:,f_dim:].to(self.device)

        return outputs, batch_y
