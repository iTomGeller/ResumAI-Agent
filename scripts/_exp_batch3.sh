#!/bin/bash
# Batch 3 (long): EXP-2 chunk grid, then EXP-7 replan sweep. Serialized because
# both bounce containers.
set -u
cd /opt/resumai-src
bash scripts/_exp2_chunk_grid.sh 2>&1 | tee /tmp/exp2_grid.log
echo "=================================================="
bash scripts/_exp7_replan_sweep.sh 2>&1 | tee /tmp/exp7_sweep.log
echo BATCH3_DONE
