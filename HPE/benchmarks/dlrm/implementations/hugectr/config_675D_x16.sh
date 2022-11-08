## DL params
export BATCH_SIZE=55296
export DGXNGPU=8

export CONFIG="675D_a100_x16.py"

## System run parms
export DGXNNODES=16
export DGXSYSTEM=675D # $(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
WALLTIME_BASE=$(( 5 + 30 * ${API_LOGGING:-0} ))
WALLTIME_MINUTES=5
export WALLTIME=9999 #$(( WALLTIME_BASE + (${NEXP:-1} * WALLTIME_MINUTES) ))
export OMPI_MCA_btl="^openib"
export MOUNTS=/raid:/raid
export CUDA_DEVICE_MAX_CONNECTIONS=3
