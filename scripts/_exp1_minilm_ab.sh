#!/bin/bash
# EXP-1 control arm: switch live stack to local MiniLM-384, reindex, benchmark,
# then switch back to bailian and verify. No traffic on this box — safe.
set -u
BASE=http://127.0.0.1
ENV=/opt/resumai-src/.env
cd /opt/resumai-src

set_env() { # key value
  if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}

wait_health() { # expected_provider
  for i in $(seq 1 50); do
    curl -fsS $BASE/api/health >/tmp/h.json 2>/dev/null && grep -q UP /tmp/h.json && grep -q "\"provider\":\"$1\"" /tmp/h.json && { cat /tmp/h.json; echo; return 0; }
    sleep 3
  done
  echo "HEALTH_TIMEOUT for $1"; cat /tmp/h.json 2>/dev/null; return 1
}

reindex_kb_wait() {
  curl -fsS -X POST $BASE/api/rag/knowledge-base/reindex; echo
  for i in $(seq 1 50); do
    sleep 3
    curl -fsS -X POST $BASE/api/rag/knowledge-base/search -H 'Content-Type: application/json' \
      -d '{"query":"AI Agent harness 预算","topK":3}' >/tmp/kb.json 2>/dev/null || true
    strat=$(python3 -c "import json;print(json.load(open('/tmp/kb.json')).get('strategy',''))" 2>/dev/null || echo "")
    vec=$(python3 -c "import json;print(json.load(open('/tmp/kb.json')).get('vectorHits',0))" 2>/dev/null || echo 0)
    echo "kb_wait_$i strategy=$strat vec=$vec"
    if [[ "$strat" == *hybrid* && "$vec" -gt 0 ]]; then return 0; fi
  done
  echo KB_REINDEX_TIMEOUT; return 1
}

reindex_jds() {
  python3 - <<'PY'
import json, urllib.request
BASE="http://127.0.0.1"
def http(method,url,body=None,timeout=90):
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url,data=data,method=method,headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read().decode())
res=http("GET",f"{BASE}/api/jds?page=1&pageSize=50")
items=res.get("items") or []
ok=fail=0
for it in items:
    jid=it.get("jdId")
    try:
        d=http("GET",f"{BASE}/api/jds/{jid}")
        http("POST",f"{BASE}/api/jd",{"jdId":jid,"title":d.get("title") or "","category":d.get("category") or "TECH","description":d.get("description") or ""})
        ok+=1
    except Exception as e:
        fail+=1; print("fail",jid,type(e).__name__)
print(f"jd reindex ok={ok} fail={fail}")
PY
}

recreate_backend() {
  docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml up -d --no-deps --force-recreate ai-resume-backend
}

echo "########## PHASE 1: switch to local MiniLM-384 ##########"
set_env EMBEDDING_PROVIDER local
set_env EMBEDDING_MODEL all-minilm-l6-v2
set_env MILVUS_DIMENSION 384
grep -E '^(EMBEDDING_PROVIDER|EMBEDDING_MODEL|MILVUS_DIMENSION)=' "$ENV"
recreate_backend
wait_health local || exit 1
reindex_jds
reindex_kb_wait || echo "WARN: kb not hybrid under local (continuing)"

echo "########## PHASE 2: benchmark local-minilm-384 ##########"
mkdir -p reports/experiments
python3 harness/run_retrieval_benchmark.py --base $BASE --exp embedding --label local-minilm-384 --out reports/experiments || true
cat reports/experiments/retrieval_embedding_local-minilm-384.json || true

echo "########## PHASE 3: switch back to bailian te3-1024 ##########"
set_env EMBEDDING_PROVIDER bailian
set_env EMBEDDING_MODEL text-embedding-v3
set_env MILVUS_DIMENSION 1024
grep -E '^(EMBEDDING_PROVIDER|EMBEDDING_MODEL|MILVUS_DIMENSION)=' "$ENV"
recreate_backend
wait_health bailian || exit 1

echo "########## PHASE 4: verify bailian retrieval intact ##########"
curl -fsS -X POST $BASE/api/rag/knowledge-base/search -H 'Content-Type: application/json' \
  -d '{"query":"AI Agent harness 预算","topK":3}' >/tmp/kb2.json || true
python3 -c "import json;d=json.load(open('/tmp/kb2.json'));print('bailian kb strategy',d.get('strategy'),'lex',d.get('lexicalHits'),'vec',d.get('vectorHits'))"
python3 harness/run_retrieval_benchmark.py --base $BASE --exp embedding --label bailian-te3-1024-rerun --out reports/experiments || true
echo "########## DONE ##########"
ls -la reports/experiments/ | grep embedding
