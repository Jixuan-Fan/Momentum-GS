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
import random
import json
import torch
import numpy as np
from utils.system_utils import searchForMaxIteration
from scene.dataset_readers import sceneLoadTypeCallbacks
from scene.gaussian_model import GaussianModel
from arguments import ModelParams
from utils.camera_utils import cameraList_from_camInfos, camera_to_JSON
from utils.distributed_utils import get_rank
from utils.load_view_utils import DataLoader

class Scene:

    gaussians : GaussianModel

    def __init__(self, args : ModelParams, gaussians : GaussianModel, load_iteration=None, shuffle=True, resolution_scales=[1.0], ply_path=None, distributed=False, block_id=-1, woimage=False, val=False):
        """b
        :param path: Path to colmap scene main folder.
        """
        rank = get_rank()
        self.model_path = args.model_path
        self.loaded_iter = None
        self.gaussians = gaussians

        if load_iteration:
            if load_iteration == -1:
                self.loaded_iter = searchForMaxIteration(os.path.join(self.model_path, "point_cloud"))
            else:
                self.loaded_iter = load_iteration
                
            print("Loading trained model at iteration {}".format(self.loaded_iter))

        self.train_cameras = {}
        self.test_cameras = {}
        self.val_cameras = {}

        partition = None
        if distributed:
            partition_name = args.partition_name
            if block_id == -1:
                block_id = get_rank()
            if block_id == 0:
                print(f"Using Partition File {partition_name}.npy")
            partition = np.load(os.path.join(args.source_path, f"data_partitions", f"{partition_name}.npy"))
            partition = partition[:, block_id]
                
        if os.path.exists(os.path.join(args.source_path, "sparse")):
            scene_info = sceneLoadTypeCallbacks["Colmap"](args.source_path, args.images, args.eval, args.lod, meganerf_partition=args.meganerf_partition, train_val_partition=args.train_val_partition, train_test_partition=args.train_test_partition, partition=partition, woimage=woimage)
        elif os.path.exists(os.path.join(args.source_path, "transforms_train.json")):
            print("Found transforms_train.json file, assuming Blender data set!")
            scene_info = sceneLoadTypeCallbacks["Blender"](args.source_path, args.white_background, args.eval, ply_path=ply_path)
        else:
            assert False, "Could not recognize scene type!"

        self.gaussians.set_appearance(len(scene_info.train_cameras))
        
        if not self.loaded_iter:
            if ply_path is not None:
                with open(ply_path, 'rb') as src_file, open(os.path.join(self.model_path, "input.ply") , 'wb') as dest_file:
                    dest_file.write(src_file.read())
            else:
                with open(scene_info.ply_path, 'rb') as src_file, open(os.path.join(self.model_path, "input.ply") , 'wb') as dest_file:
                    dest_file.write(src_file.read())
            json_cams = []
            camlist = []
            if scene_info.test_cameras:
                camlist.extend(scene_info.test_cameras)
            if scene_info.train_cameras:
                camlist.extend(scene_info.train_cameras)
            for id, cam in enumerate(camlist):
                json_cams.append(camera_to_JSON(id, cam))
            with open(os.path.join(self.model_path, "cameras.json"), 'w') as file:
                json.dump(json_cams, file)

        self.cameras_extent = scene_info.nerf_normalization["radius"]
        
        for resolution_scale in resolution_scales:
            self.train_cameras[resolution_scale] = DataLoader(args, scene_info.train_cameras, resolution_scale, shuffle=shuffle)
            self.test_cameras[resolution_scale] = DataLoader(args, scene_info.test_cameras, resolution_scale, shuffle=False)
            if val:
                self.val_cameras[resolution_scale] = DataLoader(args, scene_info.val_cameras, resolution_scale, shuffle=False)
            else:
                self.val_cameras[resolution_scale] = []

        if self.loaded_iter:
            if block_id == -1:
                self.gaussians.load_ply_sparse_gaussian(os.path.join(self.model_path, "point_cloud", "iteration_" + str(self.loaded_iter), "point_cloud.ply"))
                self.gaussians.load_mlp_checkpoints(os.path.join(self.model_path, "point_cloud", "iteration_" + str(self.loaded_iter)))
            else:
                pretrained_model_path = os.path.join(self.model_path, f"blocks/block{block_id}/iteration_{self.loaded_iter}")
                print(f'### In scene init, load block{block_id} point_cloud iteration_{self.loaded_iter} from [{pretrained_model_path}]')
                self.gaussians.load_ply_sparse_gaussian(os.path.join(pretrained_model_path, "point_cloud.ply"))

                original_mlp_path = pretrained_model_path.replace("blocks_filtered", "blocks")
                self.gaussians.load_mlp_checkpoints(original_mlp_path)
                print(f'### In scene init, load block{block_id} MLP iteration_{self.loaded_iter} from [{original_mlp_path}]')
        else:
            self.gaussians.create_from_pcd(scene_info.point_cloud, self.cameras_extent)

        self.num_train_cameras = len(self.train_cameras[resolution_scales[0]])

    def save(self, iteration, block_id=-1):
        point_cloud_path = os.path.join(self.model_path, "point_cloud/iteration_{}".format(iteration))
        os.makedirs(point_cloud_path, exist_ok=True)
        if block_id >= 0:
            if block_id == 0:
                self.gaussians.save_mlp_checkpoints(point_cloud_path)
            self.gaussians.save_ply(os.path.join(point_cloud_path, f"point_cloud_block{block_id}.ply"))
        else:
            self.gaussians.save_mlp_checkpoints(point_cloud_path)
            self.gaussians.save_ply(os.path.join(point_cloud_path, "point_cloud.ply"))

    def getTrainCameras(self, scale=1.0):
        if len(self.train_cameras[scale]) > 0:
            return self.train_cameras[scale]
        else:
            return None

    def getTestCameras(self, scale=1.0):
        if len(self.test_cameras[scale]) > 0:
            return self.test_cameras[scale]
        else:
            return None

    def getValCameras(self, scale=1.0):
        if len(self.val_cameras[scale]) > 0:
            return self.val_cameras[scale]
        else:
            return None
        
    def shutdown_dataloader(self):
        for resolution_scale in self.train_cameras:
            if self.train_cameras[resolution_scale] is not None:
                self.train_cameras[resolution_scale].shutdown()
        for resolution_scale in self.test_cameras:
            if self.test_cameras[resolution_scale] is not None:
                self.test_cameras[resolution_scale].shutdown()
        for resolution_scale in self.val_cameras:
            if self.val_cameras[resolution_scale] is not None:
                self.val_cameras[resolution_scale].shutdown()