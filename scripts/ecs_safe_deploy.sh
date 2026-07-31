#!/bin/bash
# Safe ECS deploy for ResumAI conversational agent runtime.
# - Never runs docker compose down -v / volume prune / system prune -a
# - Reuses named volumes (resumai-*-data)
# - Builds on host (mvn/npm) then docker compose with ecs override
set -euo pipefail

SRC_DIR="${SRC_DIR:-/opt/resumai-src}"
DEPLOY_ENV_SRC="${DEPLOY_ENV_SRC:-/opt/ai-resume-agent-platform/.env}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/resumai-backups}"
COMPOSE="docker compose -f docker-compose.prod.yml -f docker-compose.ecs.yml"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/${STAMP}"

log() { echo "[ecs-deploy $(date +%H:%M:%S)] $*"; }

verify_fresh_archive_manifest() (
  local manifest=".deploy-source.sha256"
  local expected_files actual_files special_path entry path
  expected_files="$(mktemp)"
  actual_files="$(mktemp)"
  trap 'rm -f -- "$expected_files" "$actual_files"' EXIT

  # A git archive may contain symlinks, but the checksum manifest only covers
  # regular-file contents. Reject links (and all other special file types)
  # rather than allowing an unhashed path into a broad Docker build context.
  special_path="$(find . -type l -print -quit)"
  if [[ -n "$special_path" ]]; then
    echo "source tree contains forbidden symbolic link: $special_path" >&2
    return 1
  fi
  special_path="$(find . ! -type d ! -type f -print -quit)"
  if [[ -n "$special_path" ]]; then
    echo "source tree contains forbidden non-regular file: $special_path" >&2
    return 1
  fi

  # Parse the sha256sum format without word splitting so ordinary spaces,
  # tabs and backslashes in tracked filenames remain part of the path.
  # Newline-containing git paths are deliberately rejected because the
  # line-oriented manifest generator cannot represent them unambiguously.
  while IFS= read -r entry || [[ -n "$entry" ]]; do
    if [[ ! "$entry" =~ ^[0-9a-fA-F]{64}\ \ .+$ ]]; then
      echo "invalid committed-source manifest entry" >&2
      return 1
    fi
    path="${entry:66}"
    if [[ -z "$path" || "$path" == /* || "$path" == "." || "$path" == ".." \
          || "$path" == ./* || "$path" == ../* || "$path" == */../* \
          || "$path" == */.. ]]; then
      echo "unsafe committed-source manifest path: $path" >&2
      return 1
    fi
    case "$path" in
      .env|.deploy-commit|.deploy-source.sha256)
        echo "deployment control file must not appear in source manifest: $path" >&2
        return 1
        ;;
    esac
    printf '%s\0' "$path" >> "$expected_files"
  done < "$manifest"

  # Only these deployment control files may exist outside the committed
  # archive. A second deploy from a previously-built directory therefore
  # fails closed; callers must sync a new fresh archive first.
  while IFS= read -r -d '' path; do
    path="${path#./}"
    case "$path" in
      .env|.deploy-commit|.deploy-source.sha256)
        continue
        ;;
    esac
    printf '%s\0' "$path" >> "$actual_files"
  done < <(find . -type f -print0)

  LC_ALL=C sort -z -o "$expected_files" "$expected_files"
  LC_ALL=C sort -z -o "$actual_files" "$actual_files"
  if ! cmp -s -- "$expected_files" "$actual_files"; then
    echo "source file set differs from committed manifest; sync a fresh archive" >&2
    echo "file-set delta (< missing from source, > unexpected in source):" >&2
    comm -3 \
      <(tr '\0' '\n' < "$expected_files") \
      <(tr '\0' '\n' < "$actual_files") | head -40 >&2 || true
    return 1
  fi

  # The set comparison above proves there are no unlisted regular files;
  # this check then proves the content of every listed file.
  sha256sum --check --strict "$manifest" >/dev/null
)

cd "$SRC_DIR"
if [[ -z "${DEPLOY_SHA:-}" ]]; then
  echo "DEPLOY_SHA is required; deploy through sync_src_and_safe_deploy.py" >&2
  exit 1
fi
GIT_SHA_FULL="$DEPLOY_SHA"
if [[ ! "$GIT_SHA_FULL" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "invalid DEPLOY_SHA; refusing non-reproducible deploy" >&2
  exit 1
fi
if [[ ! -f .deploy-commit || ! -s .deploy-source.sha256 ]]; then
  echo "missing deploy commit/checksum manifest; refusing broad build context" >&2
  exit 1
fi
SOURCE_SHA="$(tr -d '[:space:]' < .deploy-commit)"
if [[ "$SOURCE_SHA" != "$GIT_SHA_FULL" ]]; then
  echo "source marker $SOURCE_SHA does not match DEPLOY_SHA $GIT_SHA_FULL" >&2
  exit 1
fi
verify_fresh_archive_manifest
GIT_SHA="${GIT_SHA_FULL:0:12}"

log "preflight"
df -h /
free -h
nproc
docker version --format '{{.Server.Version}}'
docker compose version
docker ps -a --format 'table {{.Names}}\t{{.Status}}'
docker volume ls
log "deploy commit: $GIT_SHA_FULL"
git status -sb || true

if [[ ! -f .env ]]; then
  if [[ -f "$DEPLOY_ENV_SRC" ]]; then
    log "copying .env from $DEPLOY_ENV_SRC"
    cp -a "$DEPLOY_ENV_SRC" .env
  else
    echo "missing .env" >&2
    exit 1
  fi
fi

# Embedding defaults: Bailian is the EXP-1 winner and reachable from this ECS.
# Never silently fall back to local MiniLM for production deploys.
if grep -qE '^DASHSCOPE_API_KEY=.+' .env || grep -qE '^EMBEDDING_PROVIDER=bailian' .env; then
  grep -q '^EMBEDDING_PROVIDER=' .env || echo 'EMBEDDING_PROVIDER=bailian' >> .env
  grep -q '^EMBEDDING_MODEL=' .env || echo 'EMBEDDING_MODEL=text-embedding-v3' >> .env
  grep -q '^EMBEDDING_BASE_URL=' .env || echo 'EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1' >> .env
  log "embedding provider: bailian ($(grep '^EMBEDDING_MODEL=' .env | cut -d= -f2))"
elif grep -qE '^OPENROUTER_API_KEY=.+' .env; then
  grep -q '^EMBEDDING_PROVIDER=' .env || echo 'EMBEDDING_PROVIDER=openrouter' >> .env
  grep -q '^EMBEDDING_MODEL=' .env || echo 'EMBEDDING_MODEL=openai/text-embedding-3-small' >> .env
  log "embedding provider: openrouter ($(grep '^EMBEDDING_MODEL=' .env | cut -d= -f2))"
else
  log "WARN: no DashScope/OpenRouter key — ensure EMBEDDING_* already set in .env"
fi
grep -q '^CACHE_ENABLED=' .env || echo 'CACHE_ENABLED=true' >> .env

# Memory producer/consumer cohort telemetry must identify the code that is
# actually running.  Never carry a previous deploy's build version forward.
if grep -q '^WORKFLOW_BUILD_VERSION=' .env; then
  sed -i "s/^WORKFLOW_BUILD_VERSION=.*/WORKFLOW_BUILD_VERSION=${GIT_SHA}/" .env
else
  echo "WORKFLOW_BUILD_VERSION=${GIT_SHA}" >> .env
fi
log "workflow build version: ${GIT_SHA}"

mkdir -p "$BACKUP_DIR"
cp -a .env "$BACKUP_DIR/env.backup"
cp -a docker-compose.prod.yml "$BACKUP_DIR/"
printf '%s\n' "$GIT_SHA_FULL" > "$BACKUP_DIR/git-commit.txt"
log "backup dir: $BACKUP_DIR"

set -a
# shellcheck disable=SC1091
source .env
set +a

log "mysqldump backup (data safety)"
docker exec resumai-mysql mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
  --single-transaction --routines --triggers "$MYSQL_DATABASE" \
  > "$BACKUP_DIR/mysql-${MYSQL_DATABASE}.sql" || {
    log "WARN: mysqldump failed; continuing only if volume intact"
  }
ls -lh "$BACKUP_DIR" || true

# Count rows before deploy
BEFORE_TASKS="$(docker exec resumai-mysql mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" \
  "$MYSQL_DATABASE" -e 'SELECT COUNT(*) FROM resume_task' 2>/dev/null | tail -1 || echo unknown)"
log "resume_task rows before deploy: $BEFORE_TASKS"

log "compose config validation"
$COMPOSE config >/tmp/resumai-compose.validated.yml
grep -E 'resumai-mysql-data|resumai-redis-data' /tmp/resumai-compose.validated.yml
# The legacy graph runtime is gone — fail the deploy if it sneaks back.
LEGACY_PATTERN='workflow/'run
if grep -q "${LEGACY_PATTERN}s" /tmp/resumai-compose.validated.yml; then
  echo "legacy workflow runtime detected in compose config" >&2
  exit 1
fi

# ------------------------------------------------------------------
# Deploy drain: stop new dispatch, wait for ACTIVE runs to finish or
# checkpoint, then (after image build) restart workflow/backend and
# resume dispatch — avoids ORPHANED_ON_RESTART from mid-flight kills.
# ------------------------------------------------------------------
INTERNAL_TOKEN="${WORKFLOW_INTERNAL_TOKEN:-${INTERNAL_TOKEN:-}}"
# Backend is intentionally not published on a host port. Route internal
# lifecycle calls through the local frontend/nginx /api upstream instead.
BACKEND_DRAIN_URL="http://127.0.0.1/api/internal/agent-runs"
drain_api() {
  local method="$1" path="$2" data="${3:-}"
  if [[ -z "$INTERNAL_TOKEN" ]]; then
    return 1
  fi
  if [[ "$method" == "GET" ]]; then
    curl -fsS -H "X-Internal-Token: ${INTERNAL_TOKEN}" "${BACKEND_DRAIN_URL}${path}"
  else
    curl -fsS -X POST -H "X-Internal-Token: ${INTERNAL_TOKEN}" \
      -H "Content-Type: application/json" \
      ${data:+-d "$data"} \
      "${BACKEND_DRAIN_URL}${path}"
  fi
}

DRAIN_ARMED=0
restore_dispatch_on_exit() {
  local rc=$?
  trap - EXIT
  if [[ "$DRAIN_ARMED" == "1" ]]; then
    log "deploy exited before normal scheduler resume; attempting fail-safe resume"
    drain_api POST /resume-dispatch >/tmp/resumai-drain-recovery.json 2>/dev/null || \
      drain_api POST /drain '{"enabled":false}' >/tmp/resumai-drain-recovery.json 2>/dev/null || \
      log "WARN: fail-safe scheduler resume was unavailable"
  fi
  exit "$rc"
}
trap restore_dispatch_on_exit EXIT

if docker ps --format '{{.Names}}' | grep -q '^ai-resume-backend$'; then
  log "enable scheduler drain before rebuild/restart"
  if drain_api POST /drain '{"enabled":true}' >/tmp/resumai-drain.json; then
    DRAIN_ARMED=1
  else
    log "ERROR: drain call failed; refusing to restart a live backend"
    exit 1
  fi
  DRAIN_WAIT_SEC="${DRAIN_WAIT_SEC:-15}"
  DRAIN_READY=0
  for ((i=0; i<DRAIN_WAIT_SEC; i+=3)); do
    snap="$(drain_api GET /active 2>/dev/null || echo '')"
    if [[ -z "$snap" ]]; then
      log "ERROR: active-run drain probe unavailable; refusing unsafe restart"
      exit 1
    fi
    echo "$snap" > /tmp/resumai-active.json || true
    ready="$(python3 - <<'PY' 2>/dev/null || echo 0
import json
try:
  d=json.load(open("/tmp/resumai-active.json"))
  print(1 if d.get("readyToRestart") or int(d.get("activeCount") or 0)==0 else 0)
except Exception:
  print(0)
PY
)"
    if [[ "$ready" == "1" ]]; then
      DRAIN_READY=1
      log "drain ready (active cleared or all checkpointed) after ${i}s"
      break
    fi
    active_count="$(python3 -c 'import json;print(json.load(open("/tmp/resumai-active.json")).get("activeCount", "?"))' 2>/dev/null || echo "?")"
    log "waiting drain... active=${active_count} elapsed=${i}s"
    sleep 3
  done
  if [[ "$DRAIN_READY" != "1" ]]; then
    log "ERROR: active runs did not checkpoint within ${DRAIN_WAIT_SEC}s; refusing unsafe restart"
    exit 1
  fi
else
  log "backend not running yet — skip pre-build drain"
fi

log "Java full test suite + package (ECS only)"
cd "$SRC_DIR/backend"
# System default mvn may resolve a JDK without --release 21 support; pin JDK 21.
if [[ -d /usr/lib/jvm/java-21-openjdk-amd64 ]]; then
  export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
fi
if [[ ! -f settings.xml ]]; then
  cat > settings.xml <<'XML'
<settings>
  <mirrors>
    <mirror>
      <id>aliyun-public</id>
      <mirrorOf>central</mirrorOf>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
XML
fi
# clean is mandatory: stale target/classes would be repackaged into the jar
mvn -B -s settings.xml clean package
JAR="$(ls -1 target/resumai-agent-backend-*.jar 2>/dev/null | grep -v original | head -1)"
test -n "$JAR"
cp -f "$JAR" target/resumai-agent-backend.jar
ls -lh target/resumai-agent-backend.jar

log "fetch Temurin JRE cache if missing"
mkdir -p "$SRC_DIR/backend/.jre-cache"
JRE_CACHE="$SRC_DIR/backend/.jre-cache/temurin-21-jre.tar.gz"
if [[ ! -s "$JRE_CACHE" ]] || ! tar -tzf "$JRE_CACHE" >/dev/null 2>&1; then
  rm -f "$JRE_CACHE"
  # Reuse the verified JRE already serving production before attempting a
  # slow cross-border download. The archive keeps one top-level directory
  # because Dockerfile.ecs extracts it with --strip-components=1.
  if docker exec ai-resume-backend sh -c \
      'java_bin="$(command -v java)"; test -n "$java_bin" && test -x "$java_bin"' \
      2>/dev/null; then
    log "export JRE from current healthy backend container"
    JRE_TMP="$(mktemp -d)"
    CONTAINER_JAVA_HOME="$(docker exec ai-resume-backend sh -c \
      'java_bin="$(readlink -f "$(command -v java)")"; dirname "$(dirname "$java_bin")"')"
    case "$CONTAINER_JAVA_HOME" in
      /opt/java|/opt/java/openjdk) ;;
      *) echo "unexpected container JAVA_HOME: $CONTAINER_JAVA_HOME" >&2; exit 1 ;;
    esac
    # Repack either the legacy /opt/java/openjdk layout or the normalized
    # /opt/java layout as one top-level entry for --strip-components=1.
    docker cp "ai-resume-backend:${CONTAINER_JAVA_HOME}" "$JRE_TMP/java" >/dev/null
    tar -czf "$JRE_CACHE" -C "$JRE_TMP" java
    rm -rf "$JRE_TMP"
  else
    # Tsinghua first, GitHub fallback; both have bounded retries and total time.
    curl -fsSL --retry 2 --connect-timeout 15 --max-time 300 \
      -o "$JRE_CACHE.tmp" \
      "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/linux/OpenJDK21U-jre_x64_linux_hotspot_21.0.7_6.tar.gz" || \
    curl -fsSL --retry 2 --connect-timeout 15 --max-time 600 \
      -o "$JRE_CACHE.tmp" \
      "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.7%2B6/OpenJDK21U-jre_x64_linux_hotspot_21.0.7_6.tar.gz"
    mv "$JRE_CACHE.tmp" "$JRE_CACHE"
  fi
fi
tar -tzf "$JRE_CACHE" >/dev/null
ls -lh "$SRC_DIR/backend/.jre-cache/"

log "frontend lint + typecheck + build (ECS only)"
cd "$SRC_DIR/frontend"
npm config set registry https://registry.npmmirror.com
if [[ -f package-lock.json ]]; then
  # npm ci is preferred; ECS ships npm 9 whose optional-dep resolution can
  # disagree with a lockfile written by npm 10 — fall back to install then.
  npm ci --no-audit --no-fund || npm install --no-audit --no-fund
else
  npm install --no-audit --no-fund
fi
npm run lint
npm run build
test -d dist

log "prepare frontend ecs image context"
mkdir -p "$SRC_DIR/frontend/.nginx-cache"
# Dockerfile.ecs expects dist/ already built

log "build app images"
cd "$SRC_DIR"
$COMPOSE build ai-resume-workflow ai-resume-backend ai-resume-frontend

log "bring up stack (volumes preserved)"
$COMPOSE up -d mysql redis minio etcd milvus
# The original backend was launched outside the current Compose project and
# therefore cannot be recreated in place despite sharing the same fixed name.
# Remove only that orphan application container; named data/log/upload
# volumes are preserved and immediately remounted by the current service.
if docker inspect ai-resume-backend >/dev/null 2>&1; then
  BACKEND_PROJECT="$(docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project"}}' \
    ai-resume-backend 2>/dev/null || true)"
  if [[ "$BACKEND_PROJECT" != "resumai" ]]; then
    log "remove legacy unowned backend container (named volumes preserved)"
    docker rm -f ai-resume-backend >/dev/null
  fi
fi
$COMPOSE up -d ai-resume-workflow ai-resume-backend ai-resume-frontend
# Knowledge graph removed: stop a leftover neo4j container (volume untouched).
if docker ps --format '{{.Names}}' | grep -q '^resumai-neo4j$'; then
  log "stopping legacy neo4j container (volume preserved)"
  docker stop resumai-neo4j || true
fi
# The legacy workflow-postgres container is no longer part of the compose
# project. Stop it if still running from an older deployment; its volume
# resumai-workflow-postgres-data is intentionally left untouched on disk.
if docker ps --format '{{.Names}}' | grep -q '^ai-resume-workflow-postgres$'; then
  log "stopping legacy checkpoint postgres (volume preserved)"
  docker stop ai-resume-workflow-postgres || true
fi

log "wait health"
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1/api/health | grep -q UP; then
    log "public health UP (attempt $i)"
    break
  fi
  sleep 5
  if [[ "$i" -eq 60 ]]; then
    log "health failed"; docker compose -f docker-compose.prod.yml logs --tail=100 ai-resume-backend || true
    exit 1
  fi
done

log "wait workflow readiness"
for i in $(seq 1 30); do
  if docker exec ai-resume-workflow curl -fsS http://127.0.0.1:8090/ready >/dev/null 2>&1; then
    log "workflow ready (attempt $i)"
    break
  fi
  sleep 2
  if [[ "$i" -eq 30 ]]; then
    log "workflow /ready failed after $i attempts"
    docker compose -f docker-compose.prod.yml logs --tail=100 ai-resume-workflow || true
    exit 1
  fi
done

log "verify native MCP discovery is enabled and live"
docker inspect ai-resume-workflow \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -qx 'MCP_SKIP_PROBE=0'
# Force discovery directly inside the workflow container so the Java Ops
# client's shorter interactive timeout cannot cancel a legitimate slow probe.
docker exec ai-resume-workflow sh -c \
  'curl -fsS --max-time 180 \
    -H "X-Internal-Token: $WORKFLOW_INTERNAL_TOKEN" \
    "http://127.0.0.1:8090/internal/ops/runtime?probe=true"' \
  > /tmp/resumai-mcp-runtime-acceptance.json
# The public read path is now cheap and must expose the same cached registry.
curl -fsS --max-time 30 \
  'http://127.0.0.1/api/ops/mcp?recentLimit=1' \
  > /tmp/resumai-mcp-acceptance.json
python3 - <<'PY'
import json

with open("/tmp/resumai-mcp-runtime-acceptance.json", encoding="utf-8") as fh:
    runtime_payload = json.load(fh)
with open("/tmp/resumai-mcp-acceptance.json", encoding="utf-8") as fh:
    payload = json.load(fh)
runtime_mcp = runtime_payload.get("mcp") or {}
expected = {"exa", "microsoft-learn", "fetch"}
servers = payload.get("servers") or []
names = {str(item.get("name")) for item in servers if isinstance(item, dict)}
available = {
    str(item.get("name"))
    for item in servers
    if isinstance(item, dict) and item.get("status") == "AVAILABLE"
}
if names != expected:
    raise SystemExit(
        f"unexpected MCP inventory: expected={sorted(expected)} actual={sorted(names)}")
if not runtime_mcp.get("probed") or not payload.get("probed"):
    raise SystemExit("MCP registry did not complete a live initialize/tools/list probe")
if available != expected:
    raise SystemExit(
        f"not every keyless MCP server is live: "
        f"missing={sorted(expected - available)} "
        f"available={sorted(available)}")
if int(payload.get("toolCount") or 0) <= 0:
    raise SystemExit(f"no live MCP tools: toolCount={payload.get('toolCount')}")
print(
    f"MCP live: tools={payload.get('toolCount')} "
    f"available={','.join(sorted(available))}")
PY

log "resume scheduler dispatch after deploy"
if drain_api POST /resume-dispatch >/tmp/resumai-drain-off.json || \
    drain_api POST /drain '{"enabled":false}' >/tmp/resumai-drain-off.json; then
  DRAIN_ARMED=0
else
  log "scheduler resume failed"
  exit 1
fi

AFTER_TASKS="$(docker exec resumai-mysql mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" \
  "$MYSQL_DATABASE" -e 'SELECT COUNT(*) FROM resume_task' 2>/dev/null | tail -1 || echo unknown)"
log "resume_task rows after deploy: $AFTER_TASKS (before=$BEFORE_TASKS)"
if [[ "$BEFORE_TASKS" != "unknown" && "$AFTER_TASKS" != "unknown" && "$AFTER_TASKS" -lt "$BEFORE_TASKS" ]]; then
  echo "DATA LOSS DETECTED" >&2
  exit 2
fi

log "verify volumes still mounted"
docker inspect resumai-mysql --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{println}}{{end}}'
docker inspect resumai-redis --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{println}}{{end}}'

log "schema tables"
docker exec resumai-mysql mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" \
  -e "SHOW TABLES LIKE 'agent_run'; SHOW TABLES LIKE 'schema_migration'; SELECT version FROM schema_migration ORDER BY version;" \
  2>/dev/null | tail -40 || true

log "post-deploy: knowledge base seed + vector reindex (idempotent)"
python3 "$SRC_DIR/scripts/seed_knowledge_base.py" --base http://127.0.0.1 || \
  log "WARN: knowledge seed failed (retry manually)"
curl -fsS -X POST http://127.0.0.1/api/rag/knowledge-base/reindex || \
  log "WARN: kb reindex endpoint unavailable (vector store may be lexical-only)"

log "container status"
$COMPOSE ps
docker stats --no-stream
log "deploy complete"
