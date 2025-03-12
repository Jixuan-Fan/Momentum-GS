# Momentum-GS: Momentum Gaussian Self-Distillation for High-Quality Large Scene Reconstruction

[Jixuan Fan*](https://jixuan-fan.github.io/), [Wanhua Li*](https://li-wanhua.github.io/), Yifei Han, [Yansong Tang#](https://andytang15.github.io/)

**[[Project Page](https://jixuan-fan.github.io/Momentum-GS_Page/)] | [[Paper](https://arxiv.org/abs/2412.04887)]**

3D Gaussian Splatting has demonstrated notable success in large-scale scene reconstruction, but challenges persist due to high training memory consumption and storage overhead.
Hybrid representations that integrate implicit and explicit features offer a way to mitigate these limitations.
However, when applied in parallelized block-wise training, two critical issues arise since reconstruction accuracy deteriorates due to reduced data diversity when training each block independently, and parallel training restricts the number of divided blocks to the available number of GPUs.
To address these issues, we propose Momentum-GS, a novel approach that leverages momentum-based self-distillation to promote consistency and accuracy across the blocks while decoupling the number of blocks from the physical GPU count.
Our method maintains a teacher Gaussian decoder updated with momentum, ensuring a stable reference during training. This teacher provides each block with global guidance in a self-distillation manner, promoting spatial consistency in reconstruction.
To further ensure consistency across the blocks, we incorporate block weighting, dynamically adjusting each block’s weight according to its reconstruction accuracy.
Extensive experiments on large-scale scenes show that our method consistently outperforms existing techniques, achieving a 12.8\% improvement in LPIPS over CityGaussian with much fewer divided blocks and establishing a new state of the art.

# 📰 News
**[2024.12]** We release the code.

# 📝 TODO
- [x] Release pretrained checkpoints.
- [x] Provide guidance for dividing scene into arbitrary blocks.
- [ ] Provide guidance for training on custom datasets.
- [ ] Add appearance modeling.

# 🏙️ Overview
![](docs/pipeline_final.png)
Our method begins by dividing the scene into multiple blocks (left), periodically sampling a subset of blocks (e.g., 4 blocks) and assigning them to available GPUs for parallel processing. The momentum Gaussian decoder provides stable global guidance to each block, ensuring consistency across blocks. To align the online Gaussians with the momentum Gaussian decoder, a consistency loss is applied. During splatting, predicted images are compared with ground truth images, and the resulting reconstruction loss is used to update the shared online Gaussian decoder. Additionally, reconstruction-guided block weighting dynamically adjusts the emphasis on each block, prioritizing underperforming blocks to enhance overall scene consistency.

![](docs/comparison.png)


# 🛠️ Installation

The following guidance works well for a machine with GeForce RTX 3090 GPU, CUDA 11.8 / 11.7, Pytorch 2.3.1 / 1.13.1 

### 1. Clone the repo

```bash
git clone https://github.com/Jixuan-Fan/Momentum-GS.git
cd Momentum-GS
```

### 2. Create environment
```bash
SET DISTUTILS_USE_SDK=1 # Windows only
conda env create --file environment.yml
conda activate momentum-gs
```
Alternatively, if the above method is too slow, you can create the environment manually.

(1) Create conda environment. 
```bash
conda create -n momentum-gs python=3.8 -y
conda activate momentum-gs
```
(2) Install pytorch. If your CUDA verison is less than 11.7, you need to install [other version of pytorch](https://pytorch.org/get-started/previous-versions/).  
```bash
conda install pytorch==1.13.1 torchvision==0.14.1 torchaudio==0.13.1 pytorch-cuda=11.7 -c pytorch -c nvidia -y
```

(3) Install dependencies
```bash
pip install plyfile==0.8.1 tqdm einops wandb lpips laspy colorama jaxtyping opencv-python matplotlib ipykernel submodules/diff-gaussian-rasterization submodules/simple-knn
```
To install `torch_scatter`, download the appropriate version from [official wheel repository](https://pytorch-geometric.com/whl/) based on your Python, PyTorch, and CUDA versions. Then, install it using `pip`.

For example, if you are using `Python 3.8`, `torch 2.3.1`, and `CUDA 11.8`, you may download the following wheel:

[`torch_scatter-2.1.2+pt23cu118-cp38-cp38-linux_x86_64.whl`](https://data.pyg.org/whl/torch-2.3.0%2Bcu118/torch_scatter-2.1.2%2Bpt23cu118-cp38-cp38-linux_x86_64.whl)

Then, install it with:

```bash
pip install torch_scatter-2.1.2+pt23cu118-cp38-cp38-linux_x86_64.whl
```



# 💻 Usage

The following steps are structured in order.

## Prepare data

Please see [prepare_data.md](docs/prepare_data.md) for instructions. 

 
## Checkpoints

```bash
mkdir outputs
```

+ [Download from Tsinghua Cloud](https://cloud.tsinghua.edu.cn/d/8cf9ae5b1a2045a6993b/)
+ [Download from Google Drive](https://drive.google.com/drive/folders/1uknG0XSEHXuQfoCZNr1AGcqNGSskVNgc?usp=drive_link)


Next, please unzip these chepoints into `outputs/`



## Training
We train our Momentum-GS on GeForce RTX 3090, 24G VRAM is enough for default setting.

```bash
bash script/train/train-<SCENE_NAME>-8blocks.sh <GPU_NUM> <GPU_LIST> <TMP_DIR>

# e.g.
# (1) Reconstruct Rubble with 4 GPUs
bash script/train/train-rubble-8blocks.sh 4 0,1,2,3 /home/momentum-gs/tmp
# (2) Reconstruct Rubble with 8 GPUs
bash script/train/train-rubble-8blocks.sh 8 0,1,2,3,4,5,6,7 None
```
<details>
<summary><span style="font-weight: bold;">Command Line Arguments</span></summary>

  #### \<SCENE_NAME\>
  Support `building`, `rubble`, `residence`, `sciart`, and `matrixcity`.


  #### \<GPU_NUM\>
  The number of GPUs (e.g., `4`). Note that the default number of divided blocks is `8`, and the number of blocks must be divisible by `GPU_NUM`. Therefore, in the default setting, `GPU_NUM` must be one of the following values: `[1, 2, 4, 8]`.


  #### \<GPU_LIST\>
  ID(s) of the used GPUs (e.g., `0,1,2,3` for `GPU_NUM=4`).


  #### \<TMP_DIR\>
  If `GPU_NUM == BLOCK_NUM`, you can set this as `None`. If `GPU_NUM < BLOCK_NUM`, please specify a temporary folder (e.g., `/home/momentum-gs/tmp`).

  Each GPU will only reconstruct one block simultaneously, while the other blocks must be temporarily stored on the disk. **Note**: It is essential to choose a solid-state drive (SSD) with fast read and write speeds (> 1GB/s), HDD are strongly discouraged. 
  
  As for why the blocks are moved to disk instead of memory, we found that transferring them to memory causes unknown issues that result in a decline in reconstruction quality. Despite our best efforts and numerous attempts with various methods, we could not resolve the problem. If you have a solution, please let me know! 
</details>


After training, you need to merge all the blocks. The following script will merge the blocks, render images from the test dataset, and perform evaluation.
```bash
bash merge_render_metrics.sh <OUTPUT_FOLDER>
```
<details>
<summary><span style="font-weight: bold;">Command Line Arguments</span></summary>

  #### \<OUTPUT_FOLDER\>
  Path where the trained model should be stored (```output/<dataset>/<scene>/train/<exp_name>/<time>``` by default).
</details>



## Evaluation
To evaluate the checkpoint, you can use:
```bash
bash render_metrics.sh <OUTPUT_FOLDER>
```
<details>
<summary><span style="font-weight: bold;">Command Line Arguments</span></summary>

  #### \<OUTPUT_FOLDER\>
  Path where the trained model should be stored (```output/<dataset>/<scene>/...``` by default).
</details>


# 🏷️ License
This repository is released under the MIT license.

# 🙏 Acknowledgement

Our code is built upon [3D-GS](https://github.com/graphdeco-inria/gaussian-splatting),  [Scaffold-GS](https://github.com/city-super/Scaffold-GS), and [CityGaussian](https://github.com/DekuLiuTesla/CityGaussian). We thank all these authors for their nicely open sourced code and their great contributions to the community.

# 🥰 Citation
If you find this repository helpful, please consider citing:

```
@article{fan2024momentum,
  title={Momentum-GS: Momentum Gaussian Self-Distillation for High-Quality Large Scene Reconstruction},
  author={Fan, Jixuan and Li, Wanhua and Han, Yifei and Tang, Yansong},
  journal={arXiv preprint arXiv:2412.04887},
  year={2024}
}
```
