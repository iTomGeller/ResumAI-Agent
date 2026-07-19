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

cd "$SRC_DIR"

log "preflight"
df -h /
free -h
nproc
docker version --format '{{.Server.Version}}'
docker compose version
docker ps -a --format 'table {{.Names}}\t{{.Status}}'
docker volume ls
git rev-parse --short HEAD
git status -sb

if [[ ! -f .env ]]; then
  if [[ -f "$DEPLOY_ENV_SRC" ]]; then
    log "copying .env from $DEPLOY_ENV_SRC"
    cp -a "$DEPLOY_ENV_SRC" .env
  else
    echo "missing .env" >&2
    exit 1
  fi
fi

# Ensure sandbox flags exist without printing secrets
grep -q '^SANDBOX_ENABLED=' .env || echo 'SANDBOX_ENABLED=true' >> .env
grep -q '^SANDBOX_MAX_CONCURRENT=' .env || echo 'SANDBOX_MAX_CONCURRENT=2' >> .env
grep -q '^SANDBOX_MEM_LIMIT=' .env || echo 'SANDBOX_MEM_LIMIT=384m' >> .env
grep -q '^SANDBOX_CPU=' .env || echo 'SANDBOX_CPU=0.5' >> .env
grep -q '^SANDBOX_TTL_SECONDS=' .env || echo 'SANDBOX_TTL_SECONDS=240' >> .env

# Pin the sandbox worker image to this exact commit (never latest).
GIT_SHA="$(git rev-parse --short HEAD)"
if grep -q '^SANDBOX_WORKER_TAG=' .env; then
  sed -i "s/^SANDBOX_WORKER_TAG=.*/SANDBOX_WORKER_TAG=${GIT_SHA}/" .env
else
  echo "SANDBOX_WORKER_TAG=${GIT_SHA}" >> .env
fi
log "sandbox worker image tag: resumai-sandbox-worker:${GIT_SHA}"

mkdir -p "$BACKUP_DIR"
cp -a .env "$BACKUP_DIR/env.backup"
cp -a docker-compose.prod.yml "$BACKUP_DIR/"
git rev-parse HEAD > "$BACKUP_DIR/git-commit.txt"
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

log "Java compile + package"
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
mvn -B -s settings.xml -DskipTests clean package
JAR="$(ls -1 target/resumai-agent-backend-*.jar 2>/dev/null | grep -v original | head -1)"
test -n "$JAR"
cp -f "$JAR" target/resumai-agent-backend.jar
ls -lh target/resumai-agent-backend.jar

log "fetch Temurin JRE cache if missing"
mkdir -p "$SRC_DIR/backend/.jre-cache"
if [[ ! -f "$SRC_DIR/backend/.jre-cache/temurin-21-jre.tar.gz" ]]; then
  # Tsinghua Adoptium mirror first (mainland-stable), GitHub as fallback.
  curl -fsSL --connect-timeout 15 -o "$SRC_DIR/backend/.jre-cache/temurin-21-jre.tar.gz" \
    "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/linux/OpenJDK21U-jre_x64_linux_hotspot_21.0.7_6.tar.gz" || \
  curl -fsSL -o "$SRC_DIR/backend/.jre-cache/temurin-21-jre.tar.gz" \
    "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.7%2B6/OpenJDK21U-jre_x64_linux_hotspot_21.0.7_6.tar.gz"
fi
ls -lh "$SRC_DIR/backend/.jre-cache/"

log "frontend build (on ECS only)"
cd "$SRC_DIR/frontend"
npm config set registry https://registry.npmmirror.com
if [[ -f package-lock.json ]]; then
  # npm ci is preferred; ECS ships npm 9 whose optional-dep resolution can
  # disagree with a lockfile written by npm 10 — fall back to install then.
  npm ci --no-audit --no-fund || npm install --no-audit --no-fund
else
  npm install --no-audit --no-fund
fi
npm run build
test -d dist

log "prepare frontend ecs image context"
mkdir -p "$SRC_DIR/frontend/.nginx-cache"
# Dockerfile.ecs expects dist/ already built

log "build sandbox worker image + manager + app images"
cd "$SRC_DIR"
$COMPOSE --profile build build resumai-sandbox-worker-image
$COMPOSE build resumai-sandbox-manager ai-resume-workflow ai-resume-backend ai-resume-frontend

log "bring up stack (volumes preserved)"
$COMPOSE up -d mysql redis neo4j minio etcd milvus prometheus grafana
$COMPOSE up -d resumai-sandbox-manager ai-resume-workflow ai-resume-backend ai-resume-frontend
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
  -e "SHOW TABLES LIKE 'agent_run'; SHOW TABLES LIKE 'policy_bundle'; SHOW TABLES LIKE 'schema_migration'; SELECT version FROM schema_migration ORDER BY version;" \
  2>/dev/null | tail -40 || true

log "container status"
$COMPOSE ps
docker stats --no-stream
log "deploy complete"
