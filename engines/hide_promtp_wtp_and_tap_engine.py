"""
Train and eval functions used in main.py
"""
import math
import sys
import os
import datetime
import json
from typing import Iterable
from pathlib import Path

import torch
import torch.nn.functional as F
import torch.distributed as dist
import numpy as np

from timm.utils import accuracy
from timm.optim import create_optimizer
from timm.scheduler import create_scheduler
from torch import optim
import utils
import tree_e
from torch.distributions.multivariate_normal import MultivariateNormal
import ot

global mapG
mapG = None

def train_one_epoch(model: torch.nn.Module, original_model: torch.nn.Module,
                    criterion, data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0,
                    set_training_mode=True, task_id=-1, class_mask=None, target_task_map=None, args=None, ):
    model.train(set_training_mode)
    original_model.eval()

    if args.distributed and utils.get_world_size() > 1:
        data_loader.sampler.set_epoch(epoch)

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('Lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('Loss', utils.SmoothedValue(window_size=1, fmt='{value:.4f}'))
    header = f'Train: Epoch[{epoch + 1:{int(math.log10(args.epochs)) + 1}}/{args.epochs}]'

    for input, target in metric_logger.log_every(data_loader, args.print_freq, header):
        # input = torch.cat([input[0], input[1]], dim=0)
        input = input.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)
        bsz = len(target)

        with torch.no_grad():
            if original_model is not None:
                output = original_model(input)
                logits = output['logits']

                if args.train_mask and class_mask is not None:
                    mask = []
                    for id in range(task_id + 1):
                        mask.extend(class_mask[id])
                    not_mask = np.setdiff1d(np.arange(args.nb_classes), mask)
                    not_mask = torch.tensor(not_mask, dtype=torch.int64).to(device)
                    logits = logits.index_fill(dim=1, index=not_mask, value=float('-inf'))
                    prompt_id = torch.max(logits, dim=1)[1]
                    # translate cls to task_id
                    prompt_id = torch.tensor([target_task_map[v.item()] for v in prompt_id], device=device).unsqueeze(
                        -1)
                else:
                    prompt_id = None
            else:
                raise NotImplementedError("original model is None")
        output = model(input, task_id=task_id, prompt_id=prompt_id, train=set_training_mode,
                       prompt_momentum=args.prompt_momentum)
        logits = output['logits']
        # here is the trick to mask out classes of non-current tasks
        if args.train_mask and class_mask is not None:
            mask = class_mask[task_id]
            not_mask = np.setdiff1d(np.arange(args.nb_classes), mask)
            not_mask = torch.tensor(not_mask, dtype=torch.int64).to(device)
            logits = logits.index_fill(dim=1, index=not_mask, value=float('-inf'))

        # logits, _ = torch.split(logits, [bsz, bsz], dim=0)
        loss = criterion(logits, target)  # base criterion (CrossEntropyLoss)
        
        # TODO add contrastive loss
        pre_logits = output['pre_logits']
        # pre_logits, pre_logits2 = torch.split(pre_logits, [bsz, bsz], dim=0)
        loss += orth_loss(pre_logits, target, device, args) # robustness trick
        
        # TODO add cluster loss
        loss += cluster_loss(pre_logits, target, device, args)
        
        acc1, acc5 = accuracy(logits, target, topk=(1, 5))

        if not math.isfinite(loss.item()):
            print("Loss is {}, stopping training".format(loss.item()))
            sys.exit(1)

        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)
        optimizer.step()

        torch.cuda.synchronize()
        metric_logger.update(Loss=loss.item())
        metric_logger.update(Lr=optimizer.param_groups[0]["lr"])
        metric_logger.meters['Acc@1'].update(acc1.item(), n=input.shape[0])
        metric_logger.meters['Acc@5'].update(acc5.item(), n=input.shape[0])

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate(model: torch.nn.Module, original_model: torch.nn.Module, data_loader,
             device, i=-1, task_id=-1, class_mask=None, target_task_map=None, args=None, eval_trick=False):
    criterion = torch.nn.CrossEntropyLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test: [Task {}]'.format(i + 1)

    # switch to evaluation mode
    model.eval()
    original_model.eval()

    with torch.no_grad():
        for input, target in metric_logger.log_every(data_loader, args.print_freq, header):
            input = input.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)

            # compute output
            with torch.no_grad():
                if original_model is not None:
                    output = original_model(input)
                    logits = output['logits']
                    if args.train_mask and class_mask is not None:
                        mask = []
                        for id in range(task_id + 1):
                            mask.extend(class_mask[id])
                        not_mask = np.setdiff1d(np.arange(args.nb_classes), mask)
                        not_mask = torch.tensor(not_mask, dtype=torch.int64).to(device)
                        logits = logits.index_fill(dim=1, index=not_mask, value=float('-inf'))
                    prompt_id = torch.max(logits, dim=1)[1]
                    # translate cls to task_id
                    prompt_id = torch.tensor([target_task_map[v.item()] for v in prompt_id], device=device).unsqueeze(
                        -1)
                else:
                    raise NotImplementedError("original model is None")

            output = model(input, task_id=task_id, prompt_id=prompt_id)
            features = output['features']
            logits = output['logits']
            promtp_idx = output['prompt_idx']  # tensor B x topk

            if args.task_inc and class_mask is not None:
                # adding mask to output logits
                mask = class_mask[i]
                mask = torch.tensor(mask, dtype=torch.int64).to(device)
                logits_mask = torch.ones_like(logits, device=device) * float('-inf')
                logits_mask = logits_mask.index_fill(1, mask, 0.0)
                logits = logits + logits_mask

        
            # For eval trick: 
            if eval_trick:
                MHD = MHD_cls(features, device, args)
                if mapG == None:
                    create_number_to_sublist_map(args.G)

                energy = process_MHD(MHD, logits, args)
                logits = energy

            loss = criterion(logits, target)

            acc1, acc5 = accuracy(logits, target, topk=(1, 5))
            task_inference_acc = utils.task_inference_accuracy(promtp_idx, target, target_task_map)

            metric_logger.meters['Loss'].update(loss.item())
            metric_logger.meters['Acc@1'].update(acc1.item(), n=input.shape[0])
            metric_logger.meters['Acc@5'].update(acc5.item(), n=input.shape[0])
            metric_logger.meters['Acc@task'].update(task_inference_acc.item(), n=input.shape[0])

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print(
        '* Acc@task {task_acc.global_avg:.3f} Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
        .format(task_acc=metric_logger.meters['Acc@task'],
                top1=metric_logger.meters['Acc@1'], top5=metric_logger.meters['Acc@5'],
                losses=metric_logger.meters['Loss']))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate_till_now(model: torch.nn.Module, original_model: torch.nn.Module, data_loader,
                      device, task_id=-1, class_mask=None, target_task_map=None, acc_matrix=None, args=None, eval_trick=False):
    stat_matrix = np.zeros((4, args.num_tasks))  # 3 for Acc@1, Acc@5, Loss

    for i in range(task_id + 1):
        test_stats = evaluate(model=model, original_model=original_model, data_loader=data_loader[i]['val'],
                              device=device, i=i, task_id=task_id, class_mask=class_mask, target_task_map=target_task_map,
                              args=args, eval_trick=eval_trick)

        stat_matrix[0, i] = test_stats['Acc@1']
        stat_matrix[1, i] = test_stats['Acc@5']
        stat_matrix[2, i] = test_stats['Loss']
        stat_matrix[3, i] = test_stats['Acc@task']

        acc_matrix[i, task_id] = test_stats['Acc@1']

    avg_stat = np.divide(np.sum(stat_matrix, axis=1), task_id + 1)

    diagonal = np.diag(acc_matrix)

    result_str = "[Average accuracy till task{}]\tAcc@task: {:.4f}\tAcc@1: {:.4f}\tAcc@5: {:.4f}\tLoss: {:.4f}".format(
        task_id + 1,
        avg_stat[3],
        avg_stat[0],
        avg_stat[1],
        avg_stat[2])
    if task_id > 0:
        forgetting = np.mean((np.max(acc_matrix, axis=1) -
                              acc_matrix[:, task_id])[:task_id])
        backward = np.mean((acc_matrix[:, task_id] - diagonal)[:task_id])

        result_str += "\tForgetting: {:.4f}\tBackward: {:.4f}".format(forgetting, backward)
    print(result_str)

    return test_stats


def train_and_evaluate(model: torch.nn.Module, model_without_ddp: torch.nn.Module, original_model: torch.nn.Module,
                       criterion, data_loader: Iterable, data_loader_per_cls: Iterable,
                       optimizer: torch.optim.Optimizer,
                       lr_scheduler,
                       device: torch.device,
                       class_mask=None, target_task_map=None, args=None, ):
    
    # create matrix to save end-of-task accuracies
    acc_matrix = np.zeros((args.num_tasks, args.num_tasks))
    pre_ca_acc_matrix = np.zeros((args.num_tasks, args.num_tasks))
    global cls_mean
    global cls_cov
    cls_mean = dict()
    cls_cov = dict()
    
    global org_cls_mean
    global org_cls_cov
    org_cls_mean = dict()
    org_cls_cov = dict()
    
    global old_data
    global old_labels
    old_data = torch.empty(0).to(device)
    old_labels = torch.empty(0).to(device)
    
    global current_llist
    global WSDMatrix
    WSDMatrix = torch.zeros(size=(args.nb_classes, args.nb_classes)) # computed on the original latent space
    
    global WSDMatrix_eval
    WSDMatrix_eval = torch.zeros(size=(args.nb_classes, args.nb_classes)) # computed on the original latent space
    
    
    if args.dataset == 'Split-CIFAR100':
        if args.order == 1:
            import taxanomy.cifar100.order1.taxanomy as taxonomy
    elif args.dataset == 'Split-Imagenet-R':
        if args.order == 1:
            import taxanomy.imgR.order1.taxanomy as taxonomy
    elif args.dataset == 'Split-CUB200':
        if args.order == 1:
            import taxanomy.CUB.order1.taxanomy as taxonomy
    elif args.dataset == '5-datasets':
        if args.order == 1:
            import taxanomy.FiveDataset.order1.taxanomy as taxonomy
    else:
        print('Have not been supported')   
        exit() 
            

    for task_id in range(args.num_tasks):
        # Create new optimizer for each task to clear optimizer status
        if task_id > 0 and args.reinit_optimizer:
            if args.larger_prompt_lr:
                # This is a simple yet effective trick that helps to learn task-specific prompt better.
                base_params = [p for name, p in model_without_ddp.named_parameters() if
                            'prompt' in name and p.requires_grad == True]
                base_fc_params = [p for name, p in model_without_ddp.named_parameters() if
                                'prompt' not in name and p.requires_grad == True]
                base_params = {'params': base_params, 'lr': args.lr, 'weight_decay': args.weight_decay}
                base_fc_params = {'params': base_fc_params, 'lr': args.lr * 0.1, 'weight_decay': args.weight_decay}
                network_params = [base_params, base_fc_params]
                optimizer = create_optimizer(args, network_params)
            else:
                optimizer = create_optimizer(args, model)
            
            if args.sched != 'constant':
                lr_scheduler, _ = create_scheduler(args, optimizer)
            elif args.sched == 'constant':
                lr_scheduler = None

        # load original model checkpoint
        if args.trained_original_model:
            original_checkpoint_path = os.path.join(args.trained_original_model,
                                                    'checkpoint/task{}_checkpoint.pth'.format(task_id + 1))
            if os.path.exists(original_checkpoint_path):
                print('Loading checkpoint from:', original_checkpoint_path)
                original_checkpoint = torch.load(original_checkpoint_path, map_location=device)
                original_model.load_state_dict(original_checkpoint['model'])
            else:
                print('No checkpoint found at:', original_checkpoint_path)
                return
        # if model already trained
        checkpoint_path = os.path.join(args.output_dir, 'checkpoint/task{}_checkpoint.pth'.format(task_id + 1))
        
        _compute_mean_org(original_model=original_model, data_loader=data_loader_per_cls, device=device, task_id=task_id,
                      class_mask=class_mask[task_id], args=args)
        
        # Transfer previous learned prompt params to the new prompt
        if args.prompt_pool and args.shared_prompt_pool:
            if task_id > 0:
                prev_start = (task_id - 1) * args.top_k
                prev_end = task_id * args.top_k

                cur_start = prev_end
                cur_end = (task_id + 1) * args.top_k

                if (prev_end > args.size) or (cur_end > args.size):
                    pass
                else:
                    cur_idx = (
                        slice(None), slice(None), slice(cur_start, cur_end)) if args.use_prefix_tune_for_e_prompt else (
                        slice(None), slice(cur_start, cur_end))
                    prev_idx = (
                        slice(None), slice(None),
                        slice(prev_start, prev_end)) if args.use_prefix_tune_for_e_prompt else (
                        slice(None), slice(prev_start, prev_end))

                    with torch.no_grad():
                        if args.distributed:
                            model.module.e_prompt.prompt.grad.zero_()
                            model.module.e_prompt.prompt[cur_idx] = model.module.e_prompt.prompt[prev_idx]
                            # optimizer.param_groups[0]['params'] = model.module.parameters()
                        else:
                            model.e_prompt.prompt.grad.zero_()
                            model.e_prompt.prompt[cur_idx] = model.e_prompt.prompt[prev_idx]
                            # optimizer.param_groups[0]['params'] = model.parameters()

        # Transfer previous learned prompt param keys to the new prompt
        if args.prompt_pool and args.shared_prompt_key:
            if task_id > 0:
                prev_start = (task_id - 1) * args.top_k
                prev_end = task_id * args.top_k

                cur_start = prev_end
                cur_end = (task_id + 1) * args.top_k

                with torch.no_grad():
                    if args.distributed:
                        model.module.e_prompt.prompt_key.grad.zero_()
                        model.module.e_prompt.prompt_key[cur_idx] = model.module.e_prompt.prompt_key[prev_idx]
                        optimizer.param_groups[0]['params'] = model.module.parameters()
                    else:
                        model.e_prompt.prompt_key.grad.zero_()
                        model.e_prompt.prompt_key[cur_idx] = model.e_prompt.prompt_key[prev_idx]
                        optimizer.param_groups[0]['params'] = model.parameters()

        current_taxonomy = taxonomy.T[task_id+1]
        current_llist = tree_e.leaf_group_to_llist(current_taxonomy, dataset_name=args.dataset)
        for epoch in range(args.epochs):
            train_stats = train_one_epoch(model=model, original_model=original_model, criterion=criterion,
                                            data_loader=data_loader[task_id]['train'], optimizer=optimizer,
                                            device=device, epoch=epoch, max_norm=args.clip_grad,
                                            set_training_mode=True, task_id=task_id, class_mask=class_mask,
                                            target_task_map=target_task_map, args=args, )

            if lr_scheduler:
                lr_scheduler.step(epoch)

        if args.prompt_momentum > 0 and task_id > 0:
            if args.use_prefix_tune_for_e_prompt:
                with torch.no_grad():
                    print(model.module.e_prompt.prompt[:, :, task_id].shape)
                    print(
                        model.module.e_prompt.prompt[:, :, 0:task_id].detach().clone().mean(dim=2, keepdim=True).shape)
                    model.module.e_prompt.prompt[:, :, task_id].copy_(
                        (1 - args.prompt_momentum) * model.module.e_prompt.prompt[:, :, task_id].detach().clone()
                        + args.prompt_momentum * model.module.e_prompt.prompt[:, :, 0:task_id].detach().clone().mean(
                            dim=2))

        # compute mean and variance
        _compute_mean(model=model, data_loader=data_loader_per_cls, device=device, task_id=task_id,
                      class_mask=class_mask[task_id], args=args)

        if task_id > 0 and not args.not_train_ca:
            pre_ca_test_stats = evaluate_till_now(model=model, original_model=original_model, data_loader=data_loader,
                                                  device=device,
                                                  task_id=task_id, class_mask=class_mask,
                                                  target_task_map=target_task_map,
                                                  acc_matrix=pre_ca_acc_matrix, args=args)

            train_task_adaptive_prediction(model, args, device, class_mask, task_id)

        test_stats = evaluate_till_now(model=model, original_model=original_model, data_loader=data_loader,
                                       device=device,
                                       task_id=task_id, class_mask=class_mask, target_task_map=target_task_map,
                                       acc_matrix=acc_matrix, args=args)

        if args.output_dir and utils.is_main_process():
            Path(os.path.join(args.output_dir, 'checkpoint')).mkdir(parents=True, exist_ok=True)

            checkpoint_path = os.path.join(args.output_dir, 'checkpoint/task{}_checkpoint.pth'.format(task_id + 1))
            state_dict = {
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'args': args,
                'WSD_matrix': WSDMatrix_eval,
                'current_llist': current_llist,
                'cls_mean': cls_mean,
                'cls_cov': cls_cov,
            }
            if args.sched is not None and args.sched != 'constant':
                state_dict['lr_scheduler'] = lr_scheduler.state_dict()

            utils.save_on_master(state_dict, checkpoint_path)

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     **{f'test_{k}': v for k, v in test_stats.items()},
                     }

        if args.output_dir and utils.is_main_process():
            with open(os.path.join(args.output_dir,
                                   '{}_stats.txt'.format(datetime.datetime.now().strftime('log_%Y_%m_%d_%H_%M'))),
                      'a') as f:
                f.write(json.dumps(log_stats) + '\n')

@torch.no_grad()
def _compute_mean_org(original_model: torch.nn.Module, data_loader: Iterable, device: torch.device, task_id, class_mask=None, args=None, ):
    original_model.eval()

    print('Computing means......', len(class_mask), class_mask)
    for cls_id in class_mask:
        data_loader_cls = data_loader[cls_id]['train']
        features_per_cls = []
        for i, (inputs, targets) in enumerate(data_loader_cls):
            inputs = inputs.to(device, non_blocking=True)
            features = original_model(inputs)['pre_logits']
            # print(features.shape)
            features_per_cls.append(features)
        features_per_cls = torch.cat(features_per_cls, dim=0)
        features_per_cls_list = [torch.zeros_like(features_per_cls, device=device) for _ in range(args.world_size)]

        dist.barrier()
        dist.all_gather(features_per_cls_list, features_per_cls)

        if args.ca_storage_efficient_method == 'covariance':
            features_per_cls = torch.cat(features_per_cls_list, dim=0)
            # print(features_per_cls.shape)
            org_cls_mean[cls_id] = features_per_cls.mean(dim=0)
            org_cls_cov[cls_id] = torch.cov(features_per_cls.T) + (torch.eye(cls_mean[cls_id].shape[-1]) * 1e-4).to(device)
        
        if args.ca_storage_efficient_method == 'variance':
            features_per_cls = torch.cat(features_per_cls_list, dim=0)
            # print(features_per_cls.shape)
            org_cls_mean[cls_id] = features_per_cls.mean(dim=0)
            org_cls_cov[cls_id] = torch.diag(torch.cov(features_per_cls.T) + (torch.eye(cls_mean[cls_id].shape[-1]) * 1e-4).to(device))
        if args.ca_storage_efficient_method == 'multi-centroid':
            from sklearn.cluster import KMeans
            n_clusters = args.n_centroids
            features_per_cls = torch.cat(features_per_cls_list, dim=0).cpu().numpy()
            kmeans = KMeans(n_clusters=n_clusters)
            kmeans.fit(features_per_cls)
            cluster_lables = kmeans.labels_
            cluster_means = []
            cluster_vars = []
            for i in range(n_clusters):
               cluster_data = features_per_cls[cluster_lables == i]
               cluster_mean = torch.tensor(np.mean(cluster_data, axis=0), dtype=torch.float64).to(device)
               cluster_var = torch.tensor(np.var(cluster_data, axis=0), dtype=torch.float64).to(device)
               cluster_means.append(cluster_mean)
               cluster_vars.append(cluster_var)
            
            org_cls_mean[cls_id] = cluster_means
            org_cls_cov[cls_id] = cluster_vars
            
    update_WSM(args)

@torch.no_grad()
def _compute_mean(model: torch.nn.Module, data_loader: Iterable, device: torch.device, task_id, class_mask=None,
                  args=None, ):
    model.eval()

    for cls_id in class_mask:
        data_loader_cls = data_loader[cls_id]['train']
        features_per_cls = []
        for i, (inputs, targets) in enumerate(data_loader_cls):
            inputs = inputs.to(device, non_blocking=True)
            features = model(inputs, task_id=task_id, train=True)['pre_logits']
            features_per_cls.append(features)
        features_per_cls = torch.cat(features_per_cls, dim=0)
        features_per_cls_list = [torch.zeros_like(features_per_cls, device=device) for _ in range(args.world_size)]

        dist.barrier()
        dist.all_gather(features_per_cls_list, features_per_cls)

        if args.ca_storage_efficient_method == 'covariance':
            features_per_cls = torch.cat(features_per_cls_list, dim=0)
            # print(features_per_cls.shape)
            cls_mean[cls_id] = features_per_cls.mean(dim=0)
            cls_cov[cls_id] = torch.cov(features_per_cls.T) + (torch.eye(cls_mean[cls_id].shape[-1]) * 1e-4).to(device)
        
        if args.ca_storage_efficient_method == 'variance':
            features_per_cls = torch.cat(features_per_cls_list, dim=0)
            # print(features_per_cls.shape)
            cls_mean[cls_id] = features_per_cls.mean(dim=0)
            cls_cov[cls_id] = torch.diag(torch.cov(features_per_cls.T) + (torch.eye(cls_mean[cls_id].shape[-1]) * 1e-4).to(device))
        if args.ca_storage_efficient_method == 'multi-centroid':
            from sklearn.cluster import KMeans
            n_clusters = args.n_centroids
            features_per_cls = torch.cat(features_per_cls_list, dim=0).cpu().numpy()
            kmeans = KMeans(n_clusters=n_clusters)
            kmeans.fit(features_per_cls)
            cluster_lables = kmeans.labels_
            cluster_means = []
            cluster_vars = []
            for i in range(n_clusters):
               cluster_data = features_per_cls[cluster_lables == i]
               cluster_mean = torch.tensor(np.mean(cluster_data, axis=0), dtype=torch.float64).to(device)
               cluster_var = torch.tensor(np.var(cluster_data, axis=0), dtype=torch.float64).to(device)
               cluster_means.append(cluster_mean)
               cluster_vars.append(cluster_var)
            
            cls_mean[cls_id] = cluster_means
            cls_cov[cls_id] = cluster_vars
            
    update_WSM_eval(args)
            
# OT-based stuffs:
@torch.no_grad()
def update_WSM(args):
    n = len(org_cls_mean)
    class_ids = org_cls_mean.keys()
    for i in class_ids:
        for j in class_ids:
            if WSDMatrix[i][j] != 0:
                continue 
            else:
                # WSDMatrix[i][j] = wsd_gmm_d(org_cls_mean[i], org_cls_mean[j], org_cls_cov[i], org_cls_cov[j], args)
                WSDMatrix[i][j] = wsd_gmm_s(org_cls_mean[i], org_cls_mean[j], org_cls_cov[i], org_cls_cov[j], args) 
                WSDMatrix[j][i] = WSDMatrix[i][j]
                
    print(WSDMatrix.max())
    return

@torch.no_grad()
def update_WSM_eval(args):
    n = len(cls_mean)
    class_ids = cls_mean.keys()
    for i in class_ids:
        for j in class_ids:
            if WSDMatrix_eval[i][j] != 0:
                continue 
            else:
                # WSDMatrix[i][j] = wsd_gmm_d(org_cls_mean[i], org_cls_mean[j], org_cls_cov[i], org_cls_cov[j], args)
                WSDMatrix_eval[i][j] = wsd_gmm_s(cls_mean[i], cls_mean[j], cls_cov[i], cls_cov[j], args) 
                WSDMatrix_eval[j][i] = WSDMatrix_eval[i][j]
                
    print(WSDMatrix_eval.max())
    return

def gaussian_wasserstein(mean1, cov1, mean2, cov2):
    delta_mean = mean1 - mean2
    cov_sqrt = torch.linalg.sqrtm(cov1 @ cov2)
    
    if torch.is_complex(cov_sqrt):
        cov_sqrt = cov_sqrt.real
    
    distance = torch.dot(delta_mean, delta_mean)
    distance += torch.trace(cov1 + cov2 - 2 * cov_sqrt)
    return torch.sqrt(distance)

def wsd_gmm_d(means1, means2, covs1, covs2, args):
    total_distance = 0.0
    for i in range(len(means1)):
        for j in range(len(means2)):
            distance = gaussian_wasserstein(means1[i], covs1[i], means2[j], covs2[j])
            total_distance += distance
    return total_distance/(len(means1)*len(means2)*1.)

def wsd_gmm_s(means1, means2, covs1 ,covs2, args):

    # Sample from the GMMs
    num_sampled_pcls = args.batch_size * 5
    samples1 = gmm_sample(means1, covs1, num_sampled_pcls)
    samples2 = gmm_sample(means2, covs2, num_sampled_pcls)

    # Compute pairwise distance matrix
    M = ot.dist(samples1.cpu().numpy(), samples2.cpu().numpy())
    a = np.ones((samples1.shape[0],)) / samples1.shape[0]
    b = np.ones((samples2.shape[0],)) / samples2.shape[0]

    # Compute optimal transport plan and Wasserstein distance
    wasserstein_distance = ot.emd2(a, b, M)
    
    return wasserstein_distance

def gmm_sample(means1, covs1, num_sampled_pcls):
    sampled_data = []
    for cluster in range(len(means1)):
        mean = means1[cluster]
        var = covs1[cluster]
        if var.mean() == 0:
            continue
        m = MultivariateNormal(mean.float(), (torch.diag(var) + 1e-4 * torch.eye(mean.shape[0]).to(mean.device)).float())
        sampled_data_single = m.sample(sample_shape=(num_sampled_pcls,))
        sampled_data.append(sampled_data_single)
        
    sampled_data = torch.cat(sampled_data, dim=0).float().cuda()
    return sampled_data

def train_task_adaptive_prediction(model: torch.nn.Module, args, device, class_mask=None, task_id=-1):
    model.train()
    run_epochs = args.crct_epochs
    crct_num = 0
    param_list = [p for n, p in model.named_parameters() if p.requires_grad and 'prompt' not in n]
    network_params = [{'params': param_list, 'lr': args.ca_lr, 'weight_decay': args.weight_decay}]
    if 'mae' in args.model or 'beit' in args.model:
        optimizer = optim.AdamW(network_params, lr=args.ca_lr / 10, weight_decay=args.weight_decay)
    else:
        optimizer = optim.SGD(network_params, lr=args.ca_lr, momentum=0.9, weight_decay=5e-4)

    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizer, T_max=run_epochs)
    criterion = torch.nn.CrossEntropyLoss().to(device)

    for i in range(task_id):
        crct_num += len(class_mask[i])
        
    latest_data = []
    latest_labels = []

    # TODO: efficiency may be improved by encapsulating sampled data into Datasets class and using distributed sampler.
    for epoch in range(run_epochs):

        sampled_data = []
        sampled_label = []
        num_sampled_pcls = args.batch_size * 5

        metric_logger = utils.MetricLogger(delimiter="  ")
        metric_logger.add_meter('Lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
        metric_logger.add_meter('Loss', utils.SmoothedValue(window_size=1, fmt='{value:.4f}'))

        if args.ca_storage_efficient_method in ['covariance', 'variance']:
            for i in range(task_id + 1):
                for c_id in class_mask[i]:
                    mean = torch.tensor(cls_mean[c_id], dtype=torch.float64).to(device)
                    cov = cls_cov[c_id].to(device)
                    if args.ca_storage_efficient_method == 'variance':
                        cov = torch.diag(cov)
                    m = MultivariateNormal(mean.float(), cov.float())
                    sampled_data_single = m.sample(sample_shape=(num_sampled_pcls,))
                    sampled_data.append(sampled_data_single)

                    sampled_label.extend([c_id] * num_sampled_pcls)

                    if i == task_id:
                        latest_data.append(sampled_data_single)
                        latest_labels.extend([c_id] * num_sampled_pcls)

        elif args.ca_storage_efficient_method == 'multi-centroid':
            for i in range(task_id + 1):
                for c_id in class_mask[i]:
                    for cluster in range(len(cls_mean[c_id])):
                        mean = cls_mean[c_id][cluster]
                        var = cls_cov[c_id][cluster]
                        if var.mean() == 0:
                            continue
                        m = MultivariateNormal(mean.float(), (torch.diag(var) + 1e-4 * torch.eye(mean.shape[0]).to(mean.device)).float())
                        sampled_data_single = m.sample(sample_shape=(num_sampled_pcls,))
                        sampled_data.append(sampled_data_single)
                        sampled_label.extend([c_id] * num_sampled_pcls)
                        
                        if i == task_id:
                            latest_data.append(sampled_data_single)
                            latest_labels.extend([c_id] * num_sampled_pcls)
        else:
            raise NotImplementedError


        sampled_data = torch.cat(sampled_data, dim=0).float().to(device)
        sampled_label = torch.tensor(sampled_label).long().to(device)
        print(sampled_data.shape)
        
        # latest_data = torch.cat(latest_data, dim=0).float().to(device)
        # latest_labels = torch.cat(latest_labels).long().to(device)
        old_data = sampled_data.detach().clone() #torch.cat((old_data, latest_data), dim=0).to(device)
        old_labels = sampled_label.detach().clone() #torch.cat((old_labels, latest_labels), dim=0).to(device)

        inputs = sampled_data
        targets = sampled_label

        sf_indexes = torch.randperm(inputs.size(0))
        inputs = inputs[sf_indexes]
        targets = targets[sf_indexes]

        for _iter in range(crct_num):
            inp = inputs[_iter * num_sampled_pcls:(_iter + 1) * num_sampled_pcls]
            tgt = targets[_iter * num_sampled_pcls:(_iter + 1) * num_sampled_pcls]
            outputs = model(inp, fc_only=True)
            logits = outputs['logits']

            if args.train_mask and class_mask is not None:
                mask = []
                for id in range(task_id + 1):
                    mask.extend(class_mask[id])
                # print(mask)
                not_mask = np.setdiff1d(np.arange(args.nb_classes), mask)
                not_mask = torch.tensor(not_mask, dtype=torch.int64).to(device)
                logits = logits.index_fill(dim=1, index=not_mask, value=float('-inf'))

            loss = criterion(logits, tgt)  # base criterion (CrossEntropyLoss)
            acc1, acc5 = accuracy(logits, tgt, topk=(1, 5))

            if not math.isfinite(loss.item()):
                print("Loss is {}, stopping training".format(loss.item()))
                sys.exit(1)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            torch.cuda.synchronize()

            metric_logger.update(Loss=loss.item())
            metric_logger.update(Lr=optimizer.param_groups[0]["lr"])
            metric_logger.meters['Acc@1'].update(acc1.item(), n=inp.shape[0])
            metric_logger.meters['Acc@5'].update(acc5.item(), n=inp.shape[0])

            # gather the stats from all processes
        metric_logger.synchronize_between_processes()
        print("Averaged stats:", metric_logger)
        scheduler.step()

def orth_loss(features, targets, device, args):
    if cls_mean:
        # orth loss of this batch
        sample_mean = []
        for k, v in cls_mean.items():
            if isinstance(v, list):
                sample_mean.extend(v)
            else:
                sample_mean.append(v)
        sample_mean = torch.stack(sample_mean, dim=0).to(device, non_blocking=True)
        M = torch.cat([sample_mean, features], dim=0)
        sim = torch.matmul(M, M.t()) / 0.8
        loss = torch.nn.functional.cross_entropy(sim, torch.range(0, sim.shape[0] - 1).long().to(device))
        return args.reg * loss
    else:
        sim = torch.matmul(features, features.t()) / 0.8
        loss = torch.nn.functional.cross_entropy(sim, torch.range(0, sim.shape[0] - 1).long().to(device))
        return args.reg * loss
        # return 0.
        
def supervised_contrastive_loss(features, labels, temperature=0.1, Gamma=None):
    # Normalize features
    features = F.normalize(features, p=2, dim=1)
    
    if Gamma != None:
        n_sample = len(features)
        weight_matrix = torch.zeros((n_sample, n_sample)).to(features.device)
        labelZ = labels.long()
        for i in range(n_sample):
            for j in range(n_sample):
                if weight_matrix[i][j] != 0:
                    continue
                weight_matrix[i][j] = Gamma[labelZ[i], labelZ[j]]
                weight_matrix[j][i] = weight_matrix[i][j]
            

        weight_matrix = weight_matrix.detach()
    else:
        n_sample = len(features)
        weight_matrix = torch.ones((n_sample, n_sample)).detach().to(features.device)
    
    # Compute similarity matrix
    sim_matrix = torch.matmul(features, features.t()) / temperature
    sim_matrix = sim_matrix * weight_matrix
    
    # Mask to remove self-comparisons
    mask = torch.eye(labels.size(0), dtype=torch.bool, device=features.device)
    
    # Create label mask for positive pairs
    label_mask = labels.unsqueeze(1) == labels.unsqueeze(0)
    
    # Exclude self-comparisons
    positive_mask = label_mask & ~mask
    if positive_mask.sum() == 0:
        return 0
    
    # Compute logits and apply log-softmax
    logits = sim_matrix - torch.max(sim_matrix, dim=1, keepdim=True)[0]
    log_prob = F.log_softmax(logits, dim=1)
    
    # Compute the supervised contrastive loss
    loss = -log_prob[positive_mask].sum() / positive_mask.sum()
    
    return loss

def subsup_loss(features, labels, label_sets, temperature=0.1, args=None):
    total_loss = 0.0
    num_sets = len(label_sets)
    
    for label_subset in label_sets:
        # Select features and labels for the current subset
        mask = torch.isin(labels, torch.tensor(label_subset, device=features.device))
        subset_features = features[mask]
        subset_labels = labels[mask]
        
        if args.OT_trick:
            Gamma = 1 / np.exp(WSDMatrix / args.delta).to(features.device)
        else:
            Gamma = torch.ones((args.nb_classes, args.nb_classes)).to(features.device)
        
        # Compute the supervised contrastive loss for the subset
        if len(subset_features) > 1:
            loss = supervised_contrastive_loss(subset_features, subset_labels, temperature, Gamma)
            total_loss += loss

    return total_loss / num_sets

def cluster_loss(features, targets, device, args):
    
    old_bs = args.batch_size * 5
    sf_indexes = torch.randperm(old_data.size(0))
    old_inputs = old_data[sf_indexes]
    old_inputs =  old_inputs[:old_bs]
    old_targets = old_labels[sf_indexes]
    old_targets = old_targets[:old_bs]
    
    features = torch.cat((features, old_inputs), dim=0)
    targets = torch.cat((targets, old_targets), dim=0)
    
    sub_loss = subsup_loss(features,  targets, current_llist, args=args)
    glob_loss =  supervised_contrastive_loss(features, targets)
        
    return args.reg_glob * glob_loss + args.reg_sub * sub_loss


## For testing... 
def mahalanobis_distance(x, mean, cov, args):
    mean, cov = mean.cuda(), cov.cuda()
    cov_inv = torch.inverse(cov + (1e-6)*torch.eye(cov.shape[0]).cuda() )
    diff = x - mean
    return torch.sqrt(torch.sum((diff @ cov_inv) * diff, dim=1))

def distance_to_gmm(x, means, covariances, args):
    distances = torch.empty(0).cuda()
    n = len(means)
    for i, (mean, cov) in enumerate(zip(means, covariances)):
        if True:
            cov = torch.diag(cov) #+ 1e-4 * torch.eye(mean.shape[0]).to(mean.device).float()
        distance = mahalanobis_distance(x, mean, cov,  args)
        # print(distance.shape)
        distances = torch.cat((distances, distance.unsqueeze(1)), dim=1)
        
    distances = distances.min(dim=1)[0]

    return distances

def MHD_cls(features, device, args):
    n = len(args.clsMean)
    keys = args.clsMean.keys()
    distance = torch.ones((features.shape[0], args.nb_classes))*1e12
    if args.ca_storage_efficient_method in ['covariance', 'variance']:
        for c_id in keys:
            mean = torch.tensor(args.clsMean[c_id], dtype=torch.float64).to(device)
            cov = args.clsCov[c_id].to(device)
            if args.ca_storage_efficient_method == 'variance':
                cov = torch.diag(cov)
            dis = mahalanobis_distance(features, mean, cov, args)
            distance[:, c_id] = dis.reshape(distance[:, c_id].shape)

    elif args.ca_storage_efficient_method == 'multi-centroid':
        for c_id in keys:
            means = args.clsMean[c_id]
            covs = args.clsCov[c_id]
            dis = distance_to_gmm(features, means, covs, args)
            distance[:, c_id] = dis.reshape(distance[:, c_id].shape)
            
    else:
            raise NotImplementedError
        
    return distance

def create_number_to_sublist_map(list_of_lists):
    number_map = {}
    for index, sublist in enumerate(list_of_lists):
        for number in sublist:
            number_map[number] = index
    
    mapG = number_map

def process_MHD(distance, logits, args):
    W_MHDs = args.W_Matric
    n = len(distance)
    processed_score = torch.zeros(distance.shape).cuda()
    logits = logits.clone().detach()
    A_filtered = range(args.nb_classes)
    B = []
    for g in args.G:
        B += g
    for i in range(n):
        dis_i = distance[i].unsqueeze(1).cuda()
        logit_i = logits[i].unsqueeze(1)
        g_total = 0
        g_nearest = 0
        d_logits_max = -1e5
        for g_list in args.G:
            dis_ig = dis_i[torch.LongTensor(g_list)]
            logit_ig = logit_i[torch.LongTensor(g_list)]
            nearest_sim, nearest_label = torch.max(logit_ig, dim=0) #torch.max(dis_i[torch.LongTensor(g_list)])
            nearest_label = g_list[nearest_label]
            map_w = W_MHDs[nearest_label][torch.LongTensor(g_list)].unsqueeze(0)
            d = map_w.mm(dis_ig)
            E_g = torch.exp(args.eta_0*nearest_sim - args.eta*d)
            g_total += E_g
            logit_i[torch.LongTensor(g_list)] = logit_i[torch.LongTensor(g_list)]*E_g


        logit_i = logit_i/ g_total
        A_filtered = [item for item in A_filtered if item not in B]
        logit_i[A_filtered] = logit_i[A_filtered] + float('-inf')

        processed_score[i] = logit_i.reshape(processed_score[i].shape)
    return processed_score


