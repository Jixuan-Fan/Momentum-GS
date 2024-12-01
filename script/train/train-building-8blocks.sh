gpu_num=$1
gpu_list=$2
checkpoint_tmp_dir=$3
block_num=8
partition_name=block8_ssim0.03
aabb="-140,-100,0,-10,900,250"
block_dim="2,1,4"
consistency_loss_weight=50
adaptive_sigma=9.0
resolution=4
test_path=mill19/building-pixsfm/val/
exp_name=building-${gpu_num}gpus-8blocks

if [ $((block_num % gpu_num)) -ne 0 ]
then
    echo "Error: block_num (${block_num}) must be divisible by gpu_num (${gpu_num})"
    exit 1
fi

ulimit -n 100000

echo "@@@ Start ${exp_name} @@@"

./train.sh -d mill19/building-pixsfm/train/ --custom_test ${test_path} --images "images" --resolution ${resolution} -l ${exp_name} --gpu_num ${gpu_num} --gpu_list ${gpu_list} --partition_name ${partition_name} --block_num ${block_num} --aabb ${aabb} --block_dim ${block_dim} --consistency_loss_weight ${consistency_loss_weight} --checkpoint_tmp_dir ${checkpoint_tmp_dir} --adaptive_sigma ${adaptive_sigma}