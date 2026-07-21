#!/bin/bash
# Batch 2: recompile backend (intent fixes), rerun EXP-6, run fixed EXP-5.
set -u
BASE=http://127.0.0.1
cd /opt/resumai-src

export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
cd backend
mvn -B -s settings.xml -DskipTests clean package || exit 1
JAR=$(ls -1 target/resumai-agent-backend-*.jar | grep -v original | head -1)
cp -f "$JAR" target/resumai-agent-backend.jar
cd /opt/resumai-src
docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml build ai-resume-backend || exit 1
docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml up -d --no-deps --force-recreate ai-resume-backend

for i in $(seq 1 50); do
  curl -fsS $BASE/api/health >/tmp/h.json 2>/dev/null && grep -q UP /tmp/h.json && { echo backend_up; break; }
  sleep 3
done

echo "########## EXP-6 rerun after classifier fixes ##########"
python3 harness/run_intent_eval.py --base $BASE || echo EXP6_FAILED

echo "########## EXP-5 memory ablation (canonical types) ##########"
python3 harness/run_memory_ablation.py --base $BASE || echo EXP5_FAILED

echo BATCH2_DONE
