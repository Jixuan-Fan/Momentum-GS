#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import copy
import torch
import torchvision
import json
import wandb
import time
import random
import numpy as np
from os import makedirs
import shutil, pathlib
from pathlib import Path
from PIL import Image
import torchvision.transforms.functional as tf
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import prefilter_voxel, render, render_with_consistency_loss
import sys
from scene import Scene, GaussianModel
from utils.general_utils import safe_state
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
from utils.distributed_utils import init_distributed_mode, dist, cleanup

torch.set_num_threads(32)


try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False


def saveRuntimeCode(dst: str) -> None:
    additionalIgnorePatterns = ['.git', '.gitignore']
    ignorePatterns = set()
    ROOT = '.'
    with open(os.path.join(ROOT, '.gitignore')) as gitIgnoreFile:
        for line in gitIgnoreFile:
            if not line.startswith('#'):
                if line.endswith('\n'):
                    line = line[:-1]
                if line.endswith('/'):
                    line = line[:-1]
                ignorePatterns.add(line)
    ignorePatterns = list(ignorePatterns)
    for additionalPattern in additionalIgnorePatterns:
        ignorePatterns.append(additionalPattern)
    log_dir = pathlib.Path(__file__).parent.resolve()
    shutil.copytree(log_dir, dst, ignore=shutil.ignore_patterns(*ignorePatterns))


def set_require_grad(model, is_require_grad):
    for param in model.parameters():
        param.requires_grad = is_require_grad


def replace_model(model_a, model_b):
    # replace model_a with model_b
    with torch.no_grad():
        for param_a, param_b in zip(model_a.parameters(), model_b.parameters()):
            param_a.data = param_b.data


def momentum_update(block_mlp, main_mlp=None, m=0.9):
    with torch.no_grad():
        for block_param, main_param in zip(block_mlp.parameters(), main_mlp.parameters()):
            main_param.data = m * main_param.data + (1 - m) * block_param.data


def sync_model_with_rank0(model):
    with torch.no_grad():
        for param in model.parameters():
            dist.broadcast(param.data, 0)


def training(dataset, opt, pipe, dataset_name, saving_iterations, debug_from, wandb=None, logger=None, ply_path=None, testing_freq=1000):
    first_iter = 0
    num_blocks = dataset.block_num
    num_gpus = dist.get_world_size()
    assert num_blocks % num_gpus == 0, "Number of blocks must be divisible by number of GPUs"
    num_blocks_per_gpu = num_blocks // num_gpus
    multi_block_per_gpu = num_blocks_per_gpu > 1
    rank = dist.get_rank()
    device = torch.device("cuda", rank)

    gaussians_list, scene_list, optimizer_state_list, block_id_list = [], [], [], []
    block_psnr_list, block_ssim_list = [], []
    block_psnr_momentum, block_ssim_momentum = [0 for _ in range(num_blocks_per_gpu)], [0 for _ in range(num_blocks_per_gpu)] 
    checkpoint_tmp_dir = dataset.checkpoint_tmp_dir
    if not os.path.exists(checkpoint_tmp_dir):
        os.makedirs(checkpoint_tmp_dir, exist_ok=True)

    for block_id in range(rank * num_blocks_per_gpu, (rank + 1) * num_blocks_per_gpu):
        print(f"### Start initializing Block {block_id} on rank {rank}")

        gaussians = GaussianModel(dataset.feat_dim, dataset.n_offsets, dataset.voxel_size, dataset.update_depth, dataset.update_init_factor, dataset.update_hierachy_factor, dataset.use_feat_bank, dataset.appearance_dim, dataset.ratio, dataset.add_opacity_dist, dataset.add_cov_dist, dataset.add_color_dist)
        
        scene = Scene(dataset, gaussians, ply_path=ply_path, shuffle=True, distributed=True, block_id=block_id, val=True)

        # if not the first block, sync mlp data with previous block
        if multi_block_per_gpu and rank == 0 and block_id != rank * num_blocks_per_gpu:
            replace_model(gaussians.mlp_color, gaussians_list[0].mlp_color)
            replace_model(gaussians.mlp_cov, gaussians_list[0].mlp_cov)
            replace_model(gaussians.mlp_opacity, gaussians_list[0].mlp_opacity)

        ### sync mlp data with rank 0
        sync_model_with_rank0(gaussians.mlp_color)
        sync_model_with_rank0(gaussians.mlp_cov)
        sync_model_with_rank0(gaussians.mlp_opacity)

        gaussians.training_setup(opt)
        gaussians.train()

        gaussians_list.append(gaussians)
        scene_list.append(scene)
        block_id_list.append(block_id)
        optimizer_state_list.append(gaussians.optimizer.state_dict())

        block_psnr_list.append([])
        block_ssim_list.append([])

    if rank == 0:
        tb_writer = prepare_output_and_logger(dataset)
        iter_start = torch.cuda.Event(enable_timing = True)
        iter_end = torch.cuda.Event(enable_timing = True)
        ema_loss_for_log = 0.0
        progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")

    # Initialize Momentum Gaussian Decoder
    momentum_mlp_color = copy.deepcopy(gaussians.mlp_color).to(device)
    momentum_mlp_cov = copy.deepcopy(gaussians.mlp_cov).to(device)
    momentum_mlp_opacity = copy.deepcopy(gaussians.mlp_opacity).to(device)

    # No gradient required
    set_require_grad(momentum_mlp_color, False)
    set_require_grad(momentum_mlp_cov, False)
    set_require_grad(momentum_mlp_opacity, False)
    
    last_gaussians = None
    first_iter += 1
    dist.barrier()
  
    iteration = first_iter
    while iteration <= opt.iterations:

        end_iter = iteration + opt.block_training_interval

        for idx in range(num_blocks_per_gpu):
            # move gaussians to GPU
            torch.cuda.empty_cache()
            gaussians = gaussians_list[idx]
            scene = scene_list[idx]
            block_id = block_id_list[idx]
            train_view_loader = scene.getTrainCameras()

            if multi_block_per_gpu and iteration != first_iter:
                checkpoint = checkpoint_tmp_dir + "chkpnt_" + str(block_id) + ".pth"
                model_params = torch.load(checkpoint)
                gaussians.restore(model_params, opt)
                gaussians.train()
                torch.cuda.synchronize()
                dist.barrier()
                torch.cuda.empty_cache()

            if end_iter == opt.iterations and idx != 0:
                if multi_block_per_gpu:
                    replace_model(gaussians.mlp_color, last_gaussians.mlp_color)
                    replace_model(gaussians.mlp_cov, last_gaussians.mlp_cov)
                    replace_model(gaussians.mlp_opacity, last_gaussians.mlp_opacity)
                gaussians.freeze_mlp()

            for cur_iter in range(iteration, end_iter):
                if not gaussians.freeze_all_mlp:
                    # replace old mlp with the current one
                    if multi_block_per_gpu and cur_iter == iteration and last_gaussians is not None:
                        replace_model(gaussians.mlp_color, last_gaussians.mlp_color)
                        replace_model(gaussians.mlp_cov, last_gaussians.mlp_cov)
                        replace_model(gaussians.mlp_opacity, last_gaussians.mlp_opacity)

                    torch.cuda.synchronize()
                    dist.barrier()

                    # update Momentum Gaussian Decoder
                    if rank == 0:
                        momentum_update(gaussians.mlp_color, momentum_mlp_color, opt.momentum_coefficient)
                        momentum_update(gaussians.mlp_cov, momentum_mlp_cov, opt.momentum_coefficient)
                        momentum_update(gaussians.mlp_opacity, momentum_mlp_opacity, opt.momentum_coefficient)

                    torch.cuda.synchronize()
                    dist.barrier()

                    # sync main mlp with rank0
                    sync_model_with_rank0(momentum_mlp_color)
                    sync_model_with_rank0(momentum_mlp_cov)
                    sync_model_with_rank0(momentum_mlp_opacity)

                if rank == 0:
                    iter_start.record()

                gaussians.update_learning_rate(cur_iter)

                bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
                background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
                
                viewpoint_cam = train_view_loader.get_view()
                viewpoint_cam.world_view_transform = viewpoint_cam.world_view_transform.to(device)
                viewpoint_cam.full_proj_transform = viewpoint_cam.full_proj_transform.to(device)
                viewpoint_cam.camera_center = viewpoint_cam.camera_center.to(device)

                # Render
                if (cur_iter - 1) == debug_from:
                    pipe.debug = True
                voxel_visible_mask = prefilter_voxel(viewpoint_cam, gaussians, pipe, background)

                retain_grad = (cur_iter < opt.update_until and cur_iter >= 0)
                if gaussians.freeze_all_mlp:
                    render_pkg = render(viewpoint_cam, gaussians, pipe, background, visible_mask=voxel_visible_mask, retain_grad=retain_grad)
                else:
                    render_pkg = render_with_consistency_loss(viewpoint_cam, gaussians, momentum_mlp_color, momentum_mlp_cov, momentum_mlp_opacity, pipe, background, visible_mask=voxel_visible_mask, retain_grad=retain_grad)
                
                image, viewspace_point_tensor, visibility_filter, offset_selection_mask, radii, scaling, opacity = render_pkg["render"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["selection_mask"], render_pkg["radii"], render_pkg["scaling"], render_pkg["neural_opacity"]

                gt_image = viewpoint_cam.original_image.cuda()
                
                Ll1 = l1_loss(image, gt_image)
                ssim_loss = (1.0 - ssim(image, gt_image))
                scaling_reg = scaling.prod(dim=1).mean()
                loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * ssim_loss + 0.01 * scaling_reg

                if not gaussians.freeze_all_mlp:
                    # consistency loss
                    consistency_loss = render_pkg['consistency_loss']

                    # Reconstruction-guided block weighting
                    with torch.no_grad():
                        if cur_iter <= 1000:
                            recons_weight = torch.tensor(1.0)
                        else:
                            cur_recons_idx = len(block_psnr_list[idx]) - 1
                            cur_psnr = block_psnr_list[idx][cur_recons_idx]
                            cur_ssim = block_ssim_list[idx][cur_recons_idx]

                            momentum_psnr = block_psnr_momentum[idx] if block_psnr_momentum[idx] != 0 else cur_psnr
                            momentum_psnr = momentum_psnr * 0.9 + cur_psnr * 0.1
                            momentum_ssim = block_ssim_momentum[idx] if block_ssim_momentum[idx] != 0 else cur_ssim
                            momentum_ssim = momentum_ssim * 0.9 + cur_ssim * 0.1

                            block_psnr_momentum[idx] = momentum_psnr
                            block_ssim_momentum[idx] = momentum_ssim
                            
                            max_psnr = momentum_psnr
                            max_ssim = momentum_ssim

                            for block_idx in range(num_blocks_per_gpu):
                                max_psnr = max(max_psnr, block_psnr_momentum[block_idx])
                                max_ssim = max(max_ssim, block_ssim_momentum[block_idx])

                            max_psnr_all = torch.zeros(num_gpus, device=device)
                            max_ssim_all = torch.zeros(num_gpus, device=device)

                            max_psnr_all[rank] = max_psnr
                            max_ssim_all[rank] = max_ssim

                            torch.cuda.synchronize()
                            dist.barrier()
                            dist.all_reduce(max_psnr_all)
                            dist.all_reduce(max_ssim_all)

                            cur_max_psnr = max_psnr_all.max()
                            cur_max_ssim = max_ssim_all.max()

                            recons_weight = torch.tensor(2.0) - torch.exp(-((cur_max_psnr - momentum_psnr)**2 + (cur_max_ssim * 10 - momentum_ssim * 10)**2) / (2 * opt.adaptive_sigma * opt.adaptive_sigma))

                    loss = (loss + consistency_loss * opt.consistency_loss_weight) * recons_weight

                loss.backward()

                if not gaussians.freeze_all_mlp:
                    torch.cuda.synchronize()
                    dist.barrier()
                    with torch.no_grad():
                        # all reduce mlp grad
                        for param in gaussians.mlp_opacity.parameters():
                            torch.distributed.all_reduce(param.grad)
                            torch.cuda.synchronize()
                            dist.barrier()
                            param.grad = param.grad / num_gpus

                        for param in gaussians.mlp_color.parameters():
                            torch.distributed.all_reduce(param.grad)
                            torch.cuda.synchronize()
                            dist.barrier()
                            param.grad = param.grad / num_gpus

                        for param in gaussians.mlp_cov.parameters():
                            torch.distributed.all_reduce(param.grad)
                            torch.cuda.synchronize()
                            dist.barrier()
                            param.grad = param.grad / num_gpus

                if rank == 0:
                    iter_end.record()

                with torch.no_grad():
                    # Progress bar
                    if rank == 0:
                        ema_loss_for_log = 0.99 * loss.item() + 0.01 * ema_loss_for_log

                        if cur_iter % 10 == 0:
                            progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}"})
                            if 10 % num_blocks_per_gpu == 0:
                                progress_bar.update(10 // num_blocks_per_gpu)
                            else:
                                if idx != 0:
                                    progress_bar.update(10 // num_blocks_per_gpu)
                                else:
                                    remain = 10 - (10 // num_blocks_per_gpu) * (num_blocks_per_gpu - 1)
                                    progress_bar.update(remain)
                        if cur_iter == opt.iterations:
                            progress_bar.close()

                    # Log and validation
                    if rank == 0:
                        training_report(tb_writer, dataset_name, cur_iter, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), scene, block_id, render, (pipe, background), wandb, logger, testing_freq=testing_freq, block_psnr_list=block_psnr_list, block_ssim_list=block_ssim_list, block_idx=idx)
                    else:
                        training_report(None, dataset_name, cur_iter, Ll1, loss, l1_loss, None, scene, block_id, render, (pipe, background), testing_freq=testing_freq, block_psnr_list=block_psnr_list, block_ssim_list=block_ssim_list, block_idx=idx)

                    # Save 
                    if (cur_iter in saving_iterations):
                        time.sleep(10 * rank)
                        if rank == 0:
                            logger.info("\n[ITER {}] Block_{} Saving Gaussians".format(cur_iter, block_id))
                        else:
                            print("\n[ITER {}] Block_{} Saving Gaussians".format(cur_iter, block_id))
                        scene.save(cur_iter, block_id=block_id)
                    
                    # densification
                    if cur_iter < opt.update_until and cur_iter > opt.start_stat:
                        # add statis
                        gaussians.training_statis(viewspace_point_tensor, opacity, visibility_filter, offset_selection_mask, voxel_visible_mask)
                        
                        # densification
                        if cur_iter > opt.update_from and cur_iter % opt.update_interval == 0:
                            gaussians.adjust_anchor(check_interval=opt.update_interval, success_threshold=opt.success_threshold, grad_threshold=opt.densify_grad_threshold, min_opacity=opt.min_opacity)
                    elif cur_iter == opt.update_until:
                        print(f"### Block {block_id} stop densification.")
                        gaussians.opacity_accum = None
                        gaussians.offset_gradient_accum = None
                        gaussians.offset_denom = None
                        torch.cuda.empty_cache()
                            
                    # Optimizer step
                    if cur_iter < opt.iterations:
                        gaussians.optimizer.step()
                        gaussians.optimizer.zero_grad(set_to_none = True)

            last_gaussians = gaussians

            if multi_block_per_gpu:
                gaussians.eval()
                torch.save(gaussians.capture(), checkpoint_tmp_dir + "chkpnt_" + str(block_id) + ".pth")
                del gaussians._anchor
                del gaussians._anchor_feat
                del gaussians._offset
                del gaussians._scaling
                del gaussians._rotation
                del gaussians._opacity
                del gaussians.max_radii2D
                del gaussians.optimizer
                del gaussians.opacity_accum
                del gaussians.offset_gradient_accum
                del gaussians.offset_denom
                del gaussians.anchor_demon
                del gaussians.spatial_lr_scale
                torch.cuda.synchronize()
                dist.barrier()

            torch.cuda.empty_cache()

        iteration += opt.block_training_interval

    for scene in scene_list:
        scene.shutdown_dataloader()
        


def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])
        
    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    return tb_writer


def training_report(tb_writer, dataset_name, iteration, Ll1, loss, l1_loss, elapsed, scene : Scene, block_id, renderFunc, renderArgs, wandb=None, logger=None, testing_freq=1000, block_psnr_list=None, block_ssim_list=None, block_idx=None):
    if rank == 0 and tb_writer:
        tb_writer.add_scalar(f'{dataset_name}/train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar(f'{dataset_name}/train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar(f'{dataset_name}/iter_time', elapsed, iteration)
    if rank == 0 and wandb is not None:
        wandb.log({"train_l1_loss":Ll1, 'train_total_loss':loss, })

    if iteration % testing_freq == 0:
        scene.gaussians.eval()
        torch.cuda.empty_cache()
        validation_configs = (
            {'name': 'test', 'cameras' : scene.getTestCameras()}, {'name': 'train', 'cameras' : scene.getValCameras()})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                ssim_test = 0.0
                
                if rank == 0 and wandb is not None:
                    gt_image_list = []
                    render_image_list = []
                    errormap_list = []

                view_loader = config['cameras']
                num_views = len(view_loader)

                for idx in range(num_views):
                    viewpoint = view_loader.get_view()
                    viewpoint.world_view_transform = viewpoint.world_view_transform.to(device)
                    viewpoint.full_proj_transform = viewpoint.full_proj_transform.to(device)
                    viewpoint.camera_center = viewpoint.camera_center.to(device)
                    voxel_visible_mask = prefilter_voxel(viewpoint, scene.gaussians, *renderArgs)
                    image = torch.clamp(renderFunc(viewpoint, scene.gaussians, *renderArgs, visible_mask=voxel_visible_mask)["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if rank == 0 and tb_writer and (idx < 30):
                        tb_writer.add_images(f'{dataset_name}/'+config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        tb_writer.add_images(f'{dataset_name}/'+config['name'] + "_view_{}/errormap".format(viewpoint.image_name), (gt_image[None]-image[None]).abs(), global_step=iteration)

                        if wandb:
                            render_image_list.append(image[None])
                            errormap_list.append((gt_image[None]-image[None]).abs())
                            
                        if iteration == testing_freq:
                            tb_writer.add_images(f'{dataset_name}/'+config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                            if wandb:
                                gt_image_list.append(gt_image[None])

                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                    ssim_test += ssim(image, gt_image).mean().double()

                psnr_test /= len(config['cameras'])
                ssim_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])

                block_psnr_list[block_idx].append(psnr_test)
                block_ssim_list[block_idx].append(ssim_test)

                if rank == 0:      
                    logger.info("\n[ITER {}] Evaluating Block{} {}: L1 {} PSNR {} SSIM {}".format(iteration, block_id, config['name'], l1_test, psnr_test, ssim_test))
                else:
                    #TODO add other ranks to logger
                    print("\n[ITER {}] Evaluating Block{} {}: L1 {} PSNR {} SSIM {}".format(iteration, block_id, config['name'], l1_test, psnr_test, ssim_test))

                if rank == 0 and tb_writer:
                    tb_writer.add_scalar(f'{dataset_name}/'+config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(f'{dataset_name}/'+config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)
                if rank == 0 and wandb is not None:
                    wandb.log({f"{config['name']}_loss_viewpoint_l1_loss":l1_test, f"{config['name']}_PSNR":psnr_test})

        if rank == 0 and tb_writer:
            tb_writer.add_scalar(f'{dataset_name}/'+'total_points', scene.gaussians.get_anchor.shape[0], iteration)
        torch.cuda.empty_cache()

        scene.gaussians.train()
    

def get_logger(path):
    import logging

    logger = logging.getLogger()
    logger.setLevel(logging.INFO) 
    fileinfo = logging.FileHandler(os.path.join(path, "outputs.log"))
    fileinfo.setLevel(logging.INFO) 
    controlshow = logging.StreamHandler()
    controlshow.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    fileinfo.setFormatter(formatter)
    controlshow.setFormatter(formatter)

    logger.addHandler(fileinfo)
    logger.addHandler(controlshow)

    return logger


if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument('--warmup', action='store_true', default=False)
    parser.add_argument('--use_wandb', action='store_true', default=False)
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument('--device', default='cuda', help='device id (i.e. 0 or 0,1 or cpu)')
    parser.add_argument('--world-size', default=4, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)

    torch.cuda.reset_peak_memory_stats()

    # Distributed training
    init_distributed_mode(args=args)
    rank = args.rank
    device = torch.device(args.device)

    dataset = args.source_path.split('/')[-1]

    if rank == 0:
        # enable logging
        model_path = args.model_path
        os.makedirs(model_path, exist_ok=True)

        logger = get_logger(model_path)
        logger.info(f'args: {args}')

        try:
            saveRuntimeCode(os.path.join(args.model_path, 'backup'))
        except:
            logger.info(f'save code failed~')
    
        exp_name = args.model_path.split('/')[-2]
    
        if args.use_wandb:
            wandb.login()
            run = wandb.init(
                # Set the project where this run will be logged
                project=f"Momentum-GS-{dataset}",
                name=exp_name,
                # Track hyperparameters and run metadata
                settings=wandb.Settings(start_method="fork"),
                config=vars(args)
            )
        else:
            wandb = None
    
        logger.info("Optimizing " + args.model_path)

    # Initialize system state (RNG)
    safe_state(args.quiet)

    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    
    # training
    if rank == 0:
        training(lp.extract(args), op.extract(args), pp.extract(args), dataset, args.save_iterations, args.debug_from, wandb, logger)
    else:
        training(lp.extract(args), op.extract(args), pp.extract(args), dataset, args.save_iterations, args.debug_from)

    max_memory = torch.cuda.max_memory_allocated()
    print(f"[rank {rank}] max vram={max_memory / (1024**2)} MB\n")

    if rank == 0:
        logger.info("\nTraining complete.")

    cleanup()
    sys.exit(0)
