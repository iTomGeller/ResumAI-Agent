#!/usr/bin/env bash
set -euo pipefail

ROOT="${RAG_EXPERIMENT_ROOT:-/tmp/rag-three-stage}"
DATA_DIR="${RAG_DATA_DIR:-$ROOT/testdata/rag_three_stage}"
MODE="${1:-all}"
RUN_LABEL="${2:-$(date +%Y%m%d_%H%M%S)}"
REPORT_DIR="$ROOT/reports/$RUN_LABEL"
MONITOR_FILE="$REPORT_DIR/ecs_monitor.csv"
MONITOR_STOP="$REPORT_DIR/monitor.stop"

mkdir -p "$REPORT_DIR" "$ROOT/vendor"

for required_file in \
  jd_catalog.json \
  jd_queries.json \
  resume_evidence_cases.json \
  knowledge_documents_live.json \
  knowledge_queries.json \
  rag_gold_spans.json; do
  if [ ! -f "$DATA_DIR/$required_file" ]; then
    echo "missing benchmark file: $DATA_DIR/$required_file" >&2
    exit 2
  fi
done

container_env() {
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$1"
}

env_value() {
  local container="$1"
  local key="$2"
  container_env "$container" | sed -n "s/^${key}=//p" | tail -1
}

if [ "$MODE" = "seed" ] || [ "$MODE" = "all" ]; then
  python3 "$ROOT/scripts/seed_rag_experiment_jds.py" \
    --base http://127.0.0.1 \
    --catalog "$DATA_DIR/jd_catalog.json" \
    --receipt "$REPORT_DIR/jd_seed_receipt.json"
fi

if [ "$MODE" = "experiment" ] || [ "$MODE" = "all" ]; then
  PYTHONPATH="$ROOT/harness" python3 "$ROOT/harness/validate_three_stage_benchmark.py" \
    --data "$DATA_DIR" \
    --out "$REPORT_DIR/data_gate.json"

  # The experiment runs on the ECS host but uses the exact credentials and
  # endpoints already injected into the healthy production containers.  Values
  # are never printed or persisted in reports.
  export DASHSCOPE_API_KEY="$(env_value ai-resume-backend DASHSCOPE_API_KEY)"
  if [ -z "$DASHSCOPE_API_KEY" ]; then
    export DASHSCOPE_API_KEY="$(env_value ai-resume-backend EMBEDDING_API_KEY)"
  fi
  embedding_base_url="$(env_value ai-resume-backend EMBEDDING_BASE_URL)"
  [ -z "$embedding_base_url" ] || export EMBEDDING_BASE_URL="$embedding_base_url"
  export DEEPSEEK_API_KEY="$(env_value ai-resume-workflow DEEPSEEK_API_KEY)"
  deepseek_api_url="$(env_value ai-resume-workflow DEEPSEEK_API_URL)"
  deepseek_model="$(env_value ai-resume-workflow DEEPSEEK_MODEL)"
  deepseek_quality_model="$(env_value ai-resume-workflow DEEPSEEK_QUALITY_MODEL)"
  [ -z "$deepseek_api_url" ] || export DEEPSEEK_API_URL="$deepseek_api_url"
  [ -z "$deepseek_model" ] || export DEEPSEEK_MODEL="$deepseek_model"
  [ -z "$deepseek_quality_model" ] || export DEEPSEEK_QUALITY_MODEL="$deepseek_quality_model"
  export PYTHONPATH="$ROOT/vendor:$ROOT/harness"

  if ! PYTHONPATH="$ROOT/vendor" python3 -c 'import jieba' >/dev/null 2>&1; then
    python3 -m pip install --disable-pip-version-check --no-cache-dir \
      --index-url https://mirrors.aliyun.com/pypi/simple/ \
      --trusted-host mirrors.aliyun.com \
      --target "$ROOT/vendor" -r "$ROOT/harness/requirements-rag-experiment.txt"
  fi

  rm -f "$MONITOR_STOP"
  sh "$ROOT/harness/monitor_ecs_benchmark.sh" "$MONITOR_FILE" "$MONITOR_STOP" 2 &
  monitor_pid=$!
  cleanup_monitor() {
    touch "$MONITOR_STOP"
    wait "$monitor_pid" 2>/dev/null || true
  }
  trap cleanup_monitor EXIT INT TERM

  python3 "$ROOT/harness/run_three_stage_rag_experiments.py" \
    --phase "${RAG_PHASE:-all}" \
    --stages "${RAG_STAGES:-all}" \
    --data "$DATA_DIR" \
    --out "$REPORT_DIR" \
    --joint-trials "${RAG_JOINT_TRIALS:-48}" \
    --joint-finalists "${RAG_JOINT_FINALISTS:-10}" \
    --seed "${RAG_SEED:-20260804}"

  if [ -d "$REPORT_DIR/raw" ]; then
    python3 "$ROOT/harness/audit_three_stage_rag_chunks.py" \
      --raw-dir "$REPORT_DIR/raw" \
      --out "$REPORT_DIR/audit" \
      --top-k 5
  fi

  cleanup_monitor
  trap - EXIT INT TERM
fi

python3 - "$REPORT_DIR/run_receipt.json" "$MODE" "$RUN_LABEL" <<'PY'
import json, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "mode": sys.argv[2],
    "runLabel": sys.argv[3],
    "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "success": True,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

echo "RAG_EXPERIMENT_REPORT=$REPORT_DIR"
