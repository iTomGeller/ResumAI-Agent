#!/bin/bash
# End-to-end acceptance on ECS against the live stack.
set -euo pipefail
SRC_DIR="${SRC_DIR:-/opt/resumai-src}"
OUT_DIR="${SRC_DIR}/reports/acceptance"
BASE="${BASE_URL:-http://127.0.0.1}"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/acceptance-$(date +%Y%m%d-%H%M%S).md"
exec > >(tee "$REPORT") 2>&1

pass=0; fail=0
check() {
  local name="$1"; shift
  if "$@"; then echo "PASS: $name"; pass=$((pass+1)); else echo "FAIL: $name"; fail=$((fail+1)); fi
}

echo "# Acceptance $(date -Is)"
echo

check "health" bash -c "curl -fsS $BASE/api/health | grep -q UP"
check "workflow ready" docker exec ai-resume-workflow curl -fsS http://127.0.0.1:8090/ready
# sandbox manager image has no curl — probe with python (same as its compose healthcheck)
check "sandbox health" docker exec resumai-sandbox-manager python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8070/health', timeout=4)"

# Volume integrity
set -a; source "$SRC_DIR/.env"; set +a
TASKS=$(docker exec resumai-mysql mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" \
  -e 'SELECT COUNT(*) FROM resume_task' 2>/dev/null | tail -1)
echo "resume_task_count=$TASKS"
check "tables agent_run" bash -c "docker exec resumai-mysql mysql -N -uroot -p\"$MYSQL_ROOT_PASSWORD\" \"$MYSQL_DATABASE\" -e \"SHOW TABLES LIKE 'agent_run'\" 2>/dev/null | grep -q agent_run"
check "tables policy_bundle" bash -c "docker exec resumai-mysql mysql -N -uroot -p\"$MYSQL_ROOT_PASSWORD\" \"$MYSQL_DATABASE\" -e \"SHOW TABLES LIKE 'policy_bundle'\" 2>/dev/null | grep -q policy_bundle"
check "mysql volume" bash -c "docker inspect resumai-mysql --format '{{range .Mounts}}{{.Name}} {{end}}' | grep -q resumai-mysql-data"
check "redis volume" bash -c "docker inspect resumai-redis --format '{{range .Mounts}}{{.Name}} {{end}}' | grep -q resumai-redis-data"

# Create conversation
CREATE=$(curl -fsS -X POST "$BASE/api/conversations" -H 'Content-Type: application/json' \
  -d '{"title":"acceptance","resumeText":"技能：Java, Spring Boot, Redis\n项目：订单系统 - Spring Boot + Redis 缓存\n工作经历：2022.01-2024.01 后端","jobDescription":"Java 后端，熟悉 Spring Boot 与 Redis"}')
echo "create=$CREATE"
CID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['conversationId'])" <<<"$CREATE")
check "conversation created" test -n "$CID"

MSG=$(curl -fsS -X POST "$BASE/api/conversations/$CID/messages" -H 'Content-Type: application/json' \
  -d "{\"clientMessageId\":\"acc-$(date +%s)\",\"content\":\"请完整评估这份简历\",\"queueMode\":\"collect\"}")
echo "msg=$MSG"
RUN_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('runId') or '')" <<<"$MSG" || true)
echo "runId=$RUN_ID"
check "run enqueued" test -n "$RUN_ID"

# Poll run status
STATUS=""
for i in $(seq 1 90); do
  BODY=$(curl -fsS "$BASE/api/runs/$RUN_ID" || true)
  STATUS=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" <<<"$BODY" 2>/dev/null || true)
  echo "poll $i status=$STATUS"
  case "$STATUS" in
    SUCCEEDED|FAILED|CANCELLED|TIMED_OUT) break ;;
  esac
  sleep 2
done
check "run finished or progressing" test -n "$STATUS"

# SSE replay endpoint
check "sse events endpoint" bash -c "curl -fsS --max-time 5 \"$BASE/sse/runs/$RUN_ID\" -H 'Accept: text/event-stream' | head -c 400 | grep -Eq 'run\\.|event:|data:'"

# COLLECT follow-up
MSG2=$(curl -fsS -X POST "$BASE/api/conversations/$CID/messages" -H 'Content-Type: application/json' \
  -d "{\"clientMessageId\":\"acc-c-$(date +%s)\",\"content\":\"补充：我还有 Kafka 实战经验\",\"queueMode\":\"collect\"}")
echo "collect=$MSG2"
check "collect accepted" bash -c "echo '$MSG2' | grep -Eq 'runId|assistantMessage|COLLECT|collect|QUEUED|RUNNING|SUCCEEDED'"

# INTERRUPT on a new long request
MSG3=$(curl -fsS -X POST "$BASE/api/conversations/$CID/messages" -H 'Content-Type: application/json' \
  -d "{\"clientMessageId\":\"acc-i1-$(date +%s)\",\"content\":\"请做深度完整评估并给出面试追问\",\"queueMode\":\"collect\"}")
sleep 1
MSG4=$(curl -fsS -X POST "$BASE/api/conversations/$CID/messages" -H 'Content-Type: application/json' \
  -d "{\"clientMessageId\":\"acc-i2-$(date +%s)\",\"content\":\"停止，改为只做时间线检查\",\"queueMode\":\"interrupt\"}")
echo "interrupt=$MSG4"
check "interrupt accepted" bash -c "echo '$MSG4' | grep -Eq 'interrupt|CANCEL|runId|assistantMessage|queueMode'"

# Feedback → policy
if [[ -n "$RUN_ID" ]]; then
  FB=$(curl -fsS -X POST "$BASE/api/runs/$RUN_ID/feedback" -H 'Content-Type: application/json' \
    -d '{"accepted":true,"ratingScore":5,"recommendationAgreed":true,"scoreDelta":0,"missedEvidenceCount":0,"unsupportedClaimCount":0,"riskJudgementCorrect":true,"comment":"acceptance feedback"}' || true)
  echo "feedback=$FB"
fi
POL=$(curl -fsS "$BASE/api/policies/statistics" || true)
echo "policies=$POL"
check "policy endpoint reachable" bash -c "echo '$POL' | grep -Eq 'policy|balanced|champion|avg|reward|\\[|\\{'"

# Sandbox security: worker image should exist; manager health shows concurrent
check "sandbox worker image" docker image inspect resumai-sandbox-worker:latest >/dev/null
NET=$(docker exec -i resumai-sandbox-manager python - <<'PY'
import docker, json
c=docker.from_env()
# ensure no sandbox containers have network
bad=[]
for ct in c.containers.list(all=True, filters={"label":"sandbox=true"}):
    nets=ct.attrs.get("NetworkSettings",{}).get("Networks") or {}
    if nets and set(nets.keys()) - {"none"}:
        bad.append(ct.name)
print("ok" if not bad else "bad:"+str(bad))
PY
)
echo "sandbox_network_check=$NET"
check "sandbox network none" test "$NET" = "ok"

# Unit/integration already expected to be run separately; record container stats
docker stats --no-stream
docker compose -f "$SRC_DIR/docker-compose.prod.yml" ps

echo
echo "SUMMARY pass=$pass fail=$fail"
[[ "$fail" -eq 0 ]]
