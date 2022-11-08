#!/bin/bash

# Minimum runs for a production run is 5
# https://github.com/mlcommons/training_policies/blob/master/training_rules.adoc#12-benchmark-results
export NEXP=${NEXP:-5}

export DGXSYSTEM=675D

## COMMON
: "${ENABLE_DALI:=False}"
: "${USE_CUDA_GRAPH:=True}"
: "${CACHE_EVAL_IMAGES:=True}"
: "${EVAL_MASK_VIRTUAL_PASTE:=True}"
: "${INCLUDE_RPN_HEAD:=True}"
: "${PRECOMPUTE_RPN_CONSTANT_TENSORS:=True}"
: "${HYBRID_LOADER:=True}"
: "${BOTTLENECK:=SpatialBottleneckWithFixedBatchNorm}"
: "${CACHE_SCALE_BIAS:=True}"

## System config params
export DGXNGPU=8
export DGXSOCKETCORES=64
export DGXNSOCKET=2
export CLEAR_CACHES=1
export DGXHT=1         # HT is on is 2, HT off is 1
export SLURM_NTASKS=${DGXNGPU}
export CUDA_VISIBLE_DEVICES="0,1,2,3,4,5,6,7"
#export MELLANOX_VISIBLE_DEVICES="0,1,2,4"
export MELLANOX_VISIBLE_DEVICES="all"
export NCCL_TOPO_FILE="/workspace/object_detection/675D_nic_affinity.xml"
