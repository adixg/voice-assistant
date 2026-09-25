#!/usr/bin/env bash
# Train on the GPU: TensorFlow needs the CUDA 12 wheels' lib dirs ahead of the
# CUDA 13 ones torch pulled in (they share folder names; see train_rina.py).
cd "$(dirname "$0")"
source .venv/bin/activate
N=$PWD/.venv/lib/python3.10/site-packages/nvidia
export LD_LIBRARY_PATH=$(ls -d $N/*/lib | grep -v "/cu13/" | tr '\n' ':')$N/cu13/lib
export TF_FORCE_GPU_ALLOW_GROWTH=true  # the model is tiny; do not reserve all 8 GB of VRAM (charged to Windows commit under WSL)
exec python train_rina.py train "$@"
