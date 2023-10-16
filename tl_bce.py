import warnings
warnings.filterwarnings('ignore')
import argparse

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score
from tqdm import tqdm
import time
import os

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

import utilities.configuration as cfg
import utilities.dataloader as dl
from utilities import evaluate
from networks import s2site
from networks.other_components import *


def train(tl_model_id, trained_model_id, fold=1, ESMv='3B', device='cpu', tensor_board=False):
    if 'noESM' not in trained_model_id:
        general_config = cfg.Model_SetUp('PPI', device=device).get_setting()
    else:
        general_config = cfg.Model_SetUp('PPI_noESM', device=device).get_setting()
        
    # get PPBS trained model
    path = general_config.save_path % (trained_model_id + '_PPBS')
    
    ckpt = torch.load(path, map_location=general_config.device)
    setting = ckpt['config_setting']
    
    # switch to PPBSTLBCE mode
    if setting.cfg.use_esm:
        setting.reset_model(new_model='BCE', tl=True)
    else:
        setting.reset_model(new_model='BCE_noESM', tl=True)
    setting.reset_environment(device=device)
    config = setting.get_setting()
    
    model, wrapper, info = s2site.initial_S2Site(config=config)
    
    dataset_table = pd.read_csv(config.dataset_table) if config.reweight is not None else None
    
    # data loading
    data_x = []
    data_y = []
    data_weight = []
    
    if tensor_board and not os.path.isdir('./tb'):
        os.mkdir('./tb')
        writer = SummaryWriter(log_dir=f'./tb/{model_full_name}', comment='_ppbs', filename_suffix=model_full_name, flush_secs=1)

    print('Train and Val dataset loading...')
    # fold[0..4]
    for i in tqdm(range(5)):
        dataset = dl.read_dataset(dataset_index=i, config=config, dataset_table=dataset_table, ESMv=ESMv)
        if config.with_atom:
            data_x.append(dataset.inputs)
        else:
            data_x.append(dataset.inputs[:4])
        data_weight.append(dataset.weight)
        data_y.append(dataset.outputs)
        
    full_start = time.time()
    for k in range(fold-1, 5):
        # always load the same weights for each tl
        model.load_state_dict(ckpt['model_state'])
        
        model_full_name = f'tlv{tl_model_id}{config.mode}' % (k+1)
        model_path = config.save_path % model_full_name
        print('==============================================')
        print(f'{model_path} starting')
        print('==============================================')
        
        if tensor_board:
            writer = SummaryWriter(log_dir=f'./tb/{model_full_name}', comment='_bce_tl', filename_suffix=model_full_name, flush_secs=1)
        
        not_k = [i for i in range(5) if i!=k]
        train_inputs = [np.concatenate([data_x[i][j] for i in not_k]) for j in range( len(data_x[0]) ) ]
        train_outputs = np.concatenate([data_y[i] for i in not_k])
        train_weights = np.concatenate([data_weight[i] for i in not_k])

        train_x, train_y, train_grouped_info = wrapper.fit(train_inputs, train_outputs, sample_weight=train_weights)
        train_mx, train_lens = train_grouped_info[0], train_grouped_info[-1]
        # Val
        val_x, val_y, val_grouped_info = wrapper.fit(data_x[k], data_y[k], sample_weight=data_weight[k])
        val_mx, val_lens = val_grouped_info[0], val_grouped_info[-1]

        # DataLoader for Model Training
        train_dataset = dl.Padded_Dataset(x=train_x, y=train_y, mx=train_mx, lens=train_lens)
        val_dataset = dl.Padded_Dataset(x=val_x, y=val_y, mx=val_mx, lens=val_lens)

        train_dataloader = DataLoader(dataset=train_dataset, batch_size=config.batch_size, shuffle=True, num_workers=config.workers)
        val_dataloader = DataLoader(dataset=val_dataset, batch_size=config.batch_size, shuffle=True, num_workers=config.workers)
        
        # train+val set-up
        device = config.device
        has_val = config.val
        val_over_ = 1
        epochs = config.epochs

        loss_fn = nn.CrossEntropyLoss()
        # default eps for tf=1e-7 while pytorch=1e-8
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-4, betas=(0.9, 0.99), eps=1e-4)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=0.5, patience=2, 
                                                           verbose=1, mode='min', threshold=0.001, 
                                                           threshold_mode='abs', cooldown=1)    
        
        train_losses = [-1]
        train_xentropies = [-1]
        train_perform = [-1]
        train_time = [-1]

        val_losses = [-1]
        val_xentropies = [-1]
        val_perform = [-1]
        val_time = [-1]
        
        # early stop
        stopper = EarlyStopping(patience=2, verbose=True, delta=0.001)

        model.reset_device(device)
        model.train()
        total = len(train_dataloader)
        
        start = time.time()
        
        iter_count = 0
        for epoch in range(epochs):
            train_loss = []
            train_xentropy = []
            train_accuracy = []
            train_start = time.time()

            batches = tqdm((train_dataloader), total=total)
            
            for x, y, mx, lens in batches:
                pred = model(x, mx)
                
                # classification loss and accuracy
                loss = 0.0
                accuracy = 0.0
                no_of_samples = 0
                tmp_pred = torch.max(model.output_act(pred), dim=-1)[1].cpu().detach().numpy()
                tmp_gt = torch.max(y, dim=-1)[1].numpy()
                
                for sample in range(pred.shape[0]):
                    loss = loss + loss_fn(pred[sample, :lens[sample]], y[sample, :lens[sample]].to(device))
                    accuracy += accuracy_score(tmp_gt[sample, :lens[sample]], tmp_pred[sample, :lens[sample]])
                    no_of_samples += 1

                loss = loss / no_of_samples
                train_accuracy.append(accuracy / no_of_samples)
                train_xentropy.append(loss.item())

                # regularization loss, lambda already added to the computation function
                if config.reg:
                    for _, param in model.named_parameters():
                        if isinstance(param, ConstraintParameter) and param.regularizer is not None:
                            loss = loss + param.compute_regularization()
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                train_loss.append(loss.item())

                for _, param in model.named_parameters():
                    if isinstance(param, ConstraintParameter) and param.constraint is not None:
                        param.data = param.apply_constraint().data

                if has_val:
                    batches.set_description(f'Fold {k+1}. Train Epoch: {epoch+1}/{epochs}. Train Processing') 
                    batches.set_postfix_str(f'Fold {k+1}. Train Batch Loss | Xentropy | Accuracy: {np.mean(train_loss):.6f} | {np.mean(train_xentropy):.6f} | {np.mean(train_accuracy):.6f}. Prev Val Loss | Xentropy | Accuracy: {val_losses[-1]:.6f} | {val_xentropies[-1]:.6f} | {val_perform[-1]:.6f}. Prev Train & Val Time: {train_time[-1]:.2f}s & {val_time[-1]:.2f}s')
                else:
                    batches.set_description(f'Fold {k+1}. Train Epoch: {epoch+1}/{epochs}. Train Processing') 
                    batches.set_postfix_str(f'Fold {k+1}. Train Batch Loss | Xentropy | Accuracdy: {np.mean(train_loss):.6f} | {np.mean(train_xentropy):.6f} | {np.mean(train_accuracy):.6f}. Prev Train Loss | Xentropy | Accuracy | Time: {train_losses[-1]:.6f} | {train_xentropies[-1]:.6f} | {train_perform[-1]:.6f} | {train_time[-1]:.2f}s')
                
                iter_count += 1
                if tensor_board:
                    writer.add_scalars("Train Losses", {'Cross Entropy': np.mean(train_xentropy), 'Loss': np.mean(train_loss)}, iter_count)
                    writer.add_scalar("Train Accuracy", np.mean(train_accuracy), iter_count)
                
            train_end = time.time()
            train_time.append(train_end-train_start)
            train_losses.append(np.mean(train_loss))
            train_xentropies.append(np.mean(train_xentropy))
            train_perform.append(np.mean(train_accuracy))

            if has_val and epoch % val_over_ == 0:
                loss, xentropy, accuracy, timing = evaluate.val_test(model, val_dataloader, loss_fn=loss_fn, device=device, current_train_epoch=epoch+1, epochs=epochs)
                val_losses.append(loss)
                val_xentropies.append(xentropy)
                val_perform.append(accuracy)
                val_time.append(timing)
                
                # Visualize necessary statistics
                if tensor_board:
                    # atom part
                    for name, param in list(model.named_parameters())[:11]:
                        if param.grad is not None:
                            writer.add_histogram(name + '_grad', param.grad, epoch+1)
                        writer.add_histogram(name + '_data', param, epoch+1)
                    
                    # atom+aa last 10 learnable weights (ignore nam, nem)
                    for name, param in list(model.named_parameters())[-10:]:
                        if param.grad is not None:
                            writer.add_histogram(name + '_grad', param.grad, epoch+1)
                        writer.add_histogram(name + '_data', param, epoch+1)

                    writer.add_scalars("Train-Val Cross Entropy", {"Train": train_xentropies[-1], "Val": xentropy}, epoch+1) 
                    writer.add_scalars("Train-Val Loss", {"Train": train_losses[-1], "Val": loss}, epoch+1)
                    writer.add_scalars("Train-Val Accuracy", {"Train": train_perform[-1], "Val": accuracy}, epoch+1) 

                if stopper(model=model, val_loss=xentropy, loop=epoch+1, other_metric=[accuracy, loss]):
                    torch.save({'model_state': model.state_dict(), 
                                'optimizer_state': optimizer.state_dict(), 
                                'train_losses': train_losses[1:],
                                'val_losses': val_losses[1:],
                                'train_xentropies': train_xentropies[1:],
                                'val_xentropies': val_xentropies[1:], 
                                'train_perform': train_perform[1:],
                                'val_perform': val_perform[1:],
                                'train_time': train_time[1:],
                                'val_time': val_time[1:],
                                'scheduler': scheduler.state_dict(),
                                'val_over': val_over_,
                                'best_epoch': stopper.loop,
                                'config_setting': setting}, 
                                model_path)

                if stopper.early_stop:
                    if tensor_board:
                        writer.close()
                    break

                scheduler.step(xentropy)
        end = time.time()

        if has_val:
            print('Train + Val takes: {:.2f}s to complete'.format(end-start))
        else:
            print('Train takes: {:.2f}s to complete'.format(end-start))

    full_end = time.time()
    if has_val:
        print('All Folds Completed. Train + Val takes: {:.2f}s to complete'.format(full_end-full_start))
    else:
        print('All Folds Completed. Train takes: {:.2f}s to complete'.format(full_end-full_start))
        
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Visualize B-cell epitopes')
    parser.add_argument('--version', dest='version', type=int, help='Model version for the trained model', required=True)
    parser.add_argument('--ESMv', dest='ESMv', type=str, default='3B', choices=['8M', '35M', '150M', '650M', '3B'], help='Version of ESM used in the ESM setting')
    parser.add_argument('--seed', dest='use_seed', type=int, default=31252, help='Use seed')
    parser.add_argument('--fold', dest='fold', type=int, default=1, help='Restart from which fold')
    
    parser.add_argument('--trained_model_id', dest='trained_model_id', type=str, default='', help='Pre-trained PPI model used during transfer learning')
    parser.add_argument('--gpu_id', dest='device', default='cpu', type=str, help='Give a gpu id to train the network if available')
    parser.add_argument('--tensor_board', dest='tensor_board', action='store_true', default=False, help='Use tensorboard to monitor the training process')
    
    args = parser.parse_args()
    if args.use_seed > -1:
        cfg.set_seed(args.use_seed)
        
    print(f'============ tlv{args.version} Training ============')
    train(tl_model_id=args.version, trained_model_id=args.trained_model_id, fold=args.fold, ESMv=args.ESMv, device=args.device, tensor_board=args.tensor_board)
    print(f'============ tlv{args.version} Completed ============')

