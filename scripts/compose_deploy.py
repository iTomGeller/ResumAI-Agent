"""Shared Docker Compose commands for fresh-schema deployments.

The current runtime schema is initialized by MySQL's entrypoint on the
versioned volume declared in ``docker-compose.prod.yml``.  Deployment helpers
must never copy an older MySQL volume into it or apply incremental SQL files.
Older volumes are left untouched for manual rollback/inspection.
"""
from __future__ import annotations

COMPOSE_FILE = "docker-compose.prod.yml"
INFRA_SERVICES = (
    "mysql", "redis", "neo4j", "minio", "etcd", "milvus",
    "ai-resume-workflow-postgres", "prometheus", "grafana",
)
APP_SERVICES = ("ai-resume-workflow", "ai-resume-backend", "ai-resume-frontend")
LEGACY_COMPOSE_PROJECT = "ai-resume-agent-platform"

# These names mirror the explicit ``name:`` entries in production Compose.
# In particular, the MySQL name is deliberately versioned and must not be
# changed back to ``resumai-mysql-data`` or populated from that legacy volume.
RESUMAI_VOLUMES: tuple[str, ...] = (
    "resumai-mysql-data-conversation-v1",
    "resumai-redis-data",
    "resumai-neo4j-data",
    "resumai-neo4j-logs",
    "resumai-neo4j-plugins",
    "resumai-minio-data",
    "resumai-etcd-data",
    "resumai-milvus-data",
    "resumai-uploads-data",
    "resumai-backend-logs",
    "resumai-prometheus-data",
    "resumai-grafana-data",
    "resumai-workflow-postgres-data",
)


def ensure_resumai_volumes_shell() -> str:
    blocks = [f"docker volume create {name} >/dev/null 2>&1 || true" for name in RESUMAI_VOLUMES]
    return " ; ".join(blocks)


def retire_legacy_stack_shell(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    """Stop old project containers without removing named volumes."""
    return (
        f"cd {deploy_dir} && "
        f"docker compose -p {LEGACY_COMPOSE_PROJECT} -f {compose_file} down --remove-orphans "
        f"2>/dev/null || true"
    )


def prepare_data_volumes_shell(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    """Create current named volumes and stop old containers without deleting volumes."""
    return " ; ".join([
        ensure_resumai_volumes_shell(),
        retire_legacy_stack_shell(deploy_dir, compose_file),
    ])


def ensure_infra_up(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    services = " ".join(INFRA_SERVICES)
    return (
        f"cd {deploy_dir} && docker compose -f {compose_file} up -d {services} "
        f"2>&1 | tail -n 40"
    )


def build_app(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    services = " ".join(APP_SERVICES)
    return (
        f"cd {deploy_dir} && docker compose -f {compose_file} build {services} "
        f"2>&1 | tail -n 40"
    )


def up_app(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    services = " ".join(APP_SERVICES)
    return (
        f"cd {deploy_dir} && docker compose -f {compose_file} up -d {services} "
        f"2>&1 | tail -n 40"
    )


def compose_ps(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    return f"cd {deploy_dir} && docker compose -f {compose_file} ps"
