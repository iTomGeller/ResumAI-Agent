#!/bin/bash
# EXP-7 driver: sweep REPLAN_CONFIDENCE_THRESHOLD across live e2e batches.
set -u
cd /opt/resumai-src
ENV=.env

set_env() {
  if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}

for T in 0.40 0.55 0.70; do
  LABEL="t$(echo "$T" | tr -d '.')"
  echo "########## threshold=$T label=$LABEL ##########"
  set_env REPLAN_CONFIDENCE_THRESHOLD "$T"
  docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml up -d --no-deps --force-recreate ai-resume-workflow
  for i in $(seq 1 40); do
    if docker exec ai-resume-workflow curl -fsS http://127.0.0.1:8090/ready >/dev/null 2>&1; then
      echo workflow_ready; break
    fi
    sleep 3
  done
  python3 harness/run_replan_sweep.py --base http://127.0.0.1 --label "$LABEL" --threshold "$T" || echo "SWEEP_${LABEL}_FAILED"
done

echo "########## restore default 0.55 ##########"
set_env REPLAN_CONFIDENCE_THRESHOLD 0.55
docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml up -d --no-deps --force-recreate ai-resume-workflow
for i in $(seq 1 40); do
  docker exec ai-resume-workflow curl -fsS http://127.0.0.1:8090/ready >/dev/null 2>&1 && { echo restored; break; }
  sleep 3
done
ls -la reports/experiments/replan_sweep_*.json
echo EXP7_DONE
