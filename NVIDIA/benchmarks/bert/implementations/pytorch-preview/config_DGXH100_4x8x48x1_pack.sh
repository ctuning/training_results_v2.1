## DL params
export BATCHSIZE=48
export PACKING_FACTOR=2
export GRADIENT_STEPS=1
export LR=0.002711
export MAX_SAMPLES_TERMINATION=4500000
export MAX_STEPS=1040
export OPT_LAMB_BETA_1=0.60466
export OPT_LAMB_BETA_2=0.85437
export START_WARMUP_STEP=0
export WARMUP_STEPS=0
export WEIGHT_DECAY_RATE=0.1
export INIT_LOSS_SCALE=1024.0

export SBATCH_NETWORK=sharp
export EXTRA_PARAMS="--dense_seq_output --pad_fmha --fused_bias_fc --fused_bias_mha --fused_dropout_add --fused_gemm_gelu --packed_samples --use_transformer_engine2 "
export PHASE=2
export EVAL_ITER_START_SAMPLES=175000
export EVAL_ITER_SAMPLES=175000
REDACTED

## System run parms
export DGXNNODES=4
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
export WALLTIME_MINUTES=8
export WALLTIME=$(( ${NEXP:-1} * ${WALLTIME_MINUTES} + 5 ))

## System config params
source $(dirname ${BASH_SOURCE[0]})/config_DGXA100_common.sh

## force to use packed trainset
export DATADIR_PHASE2=${DATADIR_PHASE2_PACKED}
