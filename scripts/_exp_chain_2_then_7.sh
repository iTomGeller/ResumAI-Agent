#!/bin/bash
# Chain driver: wait for the running EXP-2 grid to finish, then run EXP-7 sweep.
set -u
cd /opt/resumai-src

echo "=== waiting for EXP-2 grid to finish ==="
for i in $(seq 1 200); do
  if ! pgrep -f "scripts/_exp2_chunk_grid.sh" >/dev/null 2>&1; then
    echo "exp2 process gone (wait_$i)"
    break
  fi
  sleep 15
done
tail -5 /tmp/exp2_grid.log 2>/dev/null || true
ls -la reports/experiments/retrieval_chunking_*.json 2>/dev/null

echo "=== EXP-7 sweep start ==="
sed -i 's/\r$//' scripts/_exp7_replan_sweep.sh harness/run_replan_sweep.py 2>/dev/null || true
bash scripts/_exp7_replan_sweep.sh 2>&1 | tee /tmp/exp7_sweep.log
echo "=== chain done ==="
ls -la reports/experiments/replan_sweep_*.json 2>/dev/null
echo CHAIN_DONE
