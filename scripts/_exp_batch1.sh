#!/bin/bash
# Batch 1: rebuild backend+workflow with EXP knobs, then run EXP-6 / EXP-5 / EXP-9.
set -u
BASE=http://127.0.0.1
cd /opt/resumai-src

echo "########## rebuild backend (jar + image) ##########"
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
cd backend
mvn -B -s settings.xml -DskipTests clean package || exit 1
JAR=$(ls -1 target/resumai-agent-backend-*.jar | grep -v original | head -1)
cp -f "$JAR" target/resumai-agent-backend.jar
cd /opt/resumai-src
docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml build ai-resume-backend || exit 1

echo "########## rebuild workflow image (executor knob) ##########"
docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml build ai-resume-workflow || exit 1

echo "########## restart both ##########"
docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml up -d --no-deps --force-recreate ai-resume-workflow ai-resume-backend

for i in $(seq 1 50); do
  curl -fsS $BASE/api/health >/tmp/h.json 2>/dev/null && grep -q UP /tmp/h.json && { cat /tmp/h.json; echo; break; }
  sleep 3
done
for i in $(seq 1 40); do
  docker exec ai-resume-workflow curl -fsS http://127.0.0.1:8090/ready >/dev/null 2>&1 && { echo workflow_ready; break; }
  sleep 3
done

echo "########## EXP-6 intent eval ##########"
python3 harness/run_intent_eval.py --base $BASE || echo EXP6_FAILED

echo "########## EXP-5 memory ablation ##########"
python3 harness/run_memory_ablation.py --base $BASE || echo EXP5_FAILED

echo "########## EXP-9 feedback seed + reward rerun ##########"
bash scripts/_exp9_seed_feedback.sh || echo EXP9_FAILED

echo BATCH1_DONE
