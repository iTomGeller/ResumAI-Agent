#!/bin/bash
# EXP-2 driver: chunk-size x overlap grid over the seeded knowledge base.
# Per combo: reconfigure -> recreate backend -> delete seeded docs -> re-seed
# (chunks under the new params) -> wait for hybrid retrieval -> benchmark.
set -u
BASE=http://127.0.0.1
cd /opt/resumai-src
ENV=.env

set_env() {
  if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}

wait_health() {
  for i in $(seq 1 50); do
    curl -fsS $BASE/api/health >/tmp/h.json 2>/dev/null && grep -q UP /tmp/h.json && return 0
    sleep 3
  done
  echo HEALTH_TIMEOUT; return 1
}

delete_seeded_docs() {
  python3 - <<'PY'
import json, urllib.request
BASE = "http://127.0.0.1"
def http(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
import importlib.util, sys
spec = importlib.util.spec_from_file_location("seedkb", "/opt/resumai-src/scripts/seed_knowledge_base.py")
seedkb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seedkb)
seed_titles = {d["title"] for d in seedkb.DOCS}
docs = http("GET", f"{BASE}/api/rag/knowledge-base/documents").get("documents") or []
removed = 0
for d in docs:
    if str(d.get("title")) in seed_titles:
        try:
            http("DELETE", f"{BASE}/api/rag/knowledge-base/documents/{d['docId']}")
            removed += 1
        except Exception as e:
            print("delete fail", d.get("docId"), e)
print("removed", removed, "seeded docs")
PY
}

wait_hybrid() {
  for i in $(seq 1 50); do
    sleep 3
    curl -fsS -X POST $BASE/api/rag/knowledge-base/search -H 'Content-Type: application/json' \
      -d '{"query":"AI Agent harness 预算","topK":3}' >/tmp/kb.json 2>/dev/null || true
    strat=$(python3 -c "import json;print(json.load(open('/tmp/kb.json')).get('strategy',''))" 2>/dev/null || echo "")
    vec=$(python3 -c "import json;print(json.load(open('/tmp/kb.json')).get('vectorHits',0))" 2>/dev/null || echo 0)
    if [[ "$strat" == *hybrid* && "$vec" -gt 0 ]]; then echo "hybrid ok (wait_$i)"; return 0; fi
  done
  echo HYBRID_TIMEOUT; return 1
}

run_combo() { # chunk overlap
  local CH="$1"; local OV="$2"; local LABEL="grid-${CH}-${OV}"
  echo "########## combo chunk=$CH overlap=$OV ##########"
  set_env KB_CHUNK_CHARS "$CH"
  set_env KB_OVERLAP_CHARS "$OV"
  docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml up -d --no-deps --force-recreate ai-resume-backend
  wait_health || return 1
  delete_seeded_docs
  python3 scripts/seed_knowledge_base.py --base $BASE || true
  wait_hybrid || echo "WARN: $LABEL not hybrid"
  python3 harness/run_retrieval_benchmark.py --base $BASE --exp chunking --label "$LABEL" --out reports/experiments || true
}

# grid: sizes x representative overlaps (~0% / 15% / 25%)
run_combo 256 0
run_combo 256 40
run_combo 320 0
run_combo 400 60
run_combo 400 100
run_combo 512 75
run_combo 512 128
run_combo 768 115

echo "########## restore default 320/60 ##########"
run_combo 320 60
ls -la reports/experiments/retrieval_chunking_*.json
echo EXP2_DONE
