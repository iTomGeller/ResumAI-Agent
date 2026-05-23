"""Shared docker compose commands for safe redeploys (preserve data volumes)."""
from __future__ import annotations

COMPOSE_FILE = "docker-compose.prod.yml"
INFRA_SERVICES = ("mysql", "redis", "neo4j", "minio", "etcd", "milvus", "prometheus", "grafana")
APP_SERVICES = ("ai-resume-backend", "ai-resume-frontend")
LEGACY_COMPOSE_PROJECT = "ai-resume-agent-platform"

# Legacy project-prefixed volumes created before explicit `name:` entries.
LEGACY_VOLUME_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("ai-resume-agent-platform_mysql-data", "resumai-mysql-data"),
    ("ai-resume-agent-platform_redis-data", "resumai-redis-data"),
    ("ai-resume-agent-platform_neo4j-data", "resumai-neo4j-data"),
    ("ai-resume-agent-platform_neo4j-logs", "resumai-neo4j-logs"),
    ("ai-resume-agent-platform_neo4j-plugins", "resumai-neo4j-plugins"),
    ("ai-resume-agent-platform_minio-data", "resumai-minio-data"),
    ("ai-resume-agent-platform_etcd-data", "resumai-etcd-data"),
    ("ai-resume-agent-platform_milvus-data", "resumai-milvus-data"),
    ("ai-resume-agent-platform_uploads-data", "resumai-uploads-data"),
    ("ai-resume-agent-platform_backend-logs", "resumai-backend-logs"),
)

RESUMAI_VOLUMES = tuple(new for _, new in LEGACY_VOLUME_MIGRATIONS)


def ensure_resumai_volumes_shell() -> str:
    blocks = [f"docker volume create {name} >/dev/null 2>&1 || true" for name in RESUMAI_VOLUMES]
    return " ; ".join(blocks)


def migrate_legacy_volumes_shell() -> str:
    """Copy data from old compose-prefixed volumes into stable resumai-* volumes."""
    blocks: list[str] = []
    for old, new in LEGACY_VOLUME_MIGRATIONS:
        blocks.append(
            f"if docker volume inspect {old} >/dev/null 2>&1 "
            f"&& ! docker volume inspect {new} >/dev/null 2>&1; then "
            f"echo '[migrate] {old} -> {new}'; "
            f"docker volume create {new} >/dev/null; "
            f"docker run --rm -v {old}:/from:ro -v {new}:/to alpine sh -c 'cp -a /from/. /to/.'; "
            f"fi"
        )
    return " ; ".join(blocks)


def retire_legacy_stack_shell(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    """Stop old project containers without removing named volumes."""
    return (
        f"cd {deploy_dir} && "
        f"docker compose -p {LEGACY_COMPOSE_PROJECT} -f {compose_file} down --remove-orphans "
        f"2>/dev/null || true"
    )


def prepare_data_volumes_shell(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    """Migrate legacy volumes, ensure resumai-* volumes exist, retire old stack."""
    return " ; ".join([
        migrate_legacy_volumes_shell(),
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
        f"cd {deploy_dir} && docker compose -f {compose_file} up -d --no-deps {services} "
        f"2>&1 | tail -n 40"
    )


def compose_ps(deploy_dir: str, compose_file: str = COMPOSE_FILE) -> str:
    return f"cd {deploy_dir} && docker compose -f {compose_file} ps"
