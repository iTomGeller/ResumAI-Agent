"""
Deploy ResumAI-Agent (full PRD stack) to an Aliyun ECS via SSH.

Always-required environment variables (or values in `.deploy.local.env`):
  ALIYUN_HOST
  ALIYUN_PASSWORD or ALIYUN_KEY_PATH

When the remote deployment has no `.env` yet, first bootstrap also requires:
  DEEPSEEK_API_KEY
  MYSQL_ROOT_PASSWORD
  MYSQL_PASSWORD
  REDIS_PASSWORD
  NEO4J_AUTH
  MINIO_ROOT_PASSWORD
  WORKFLOW_INTERNAL_TOKEN
  WORKFLOW_POSTGRES_PASSWORD  URL-safe characters only (A-Z a-z 0-9 . _ ~ -)

Optional environment variables:
  ALIYUN_USER          default root
  ALIYUN_PORT          default 22
  DEPLOY_DIR           default /opt/ai-resume-agent-platform
  REPO_URL             default https://github.com/iTomGeller/ResumAI-Agent.git
  COMPOSE_FILE         default docker-compose.prod.yml
  REPO_BRANCH          default main
  MYSQL_DATABASE       default resumai_agent
  MYSQL_USER           default resumai
  MINIO_ROOT_USER      default resumai

This script only opens an SSH session — it never installs project
dependencies locally. Docker, Maven and npm all run on the ECS.
The default deployment launches the complete Compose stack. MySQL uses the
versioned volume declared in ``docker-compose.prod.yml`` and initializes only
from the current complete ``schema.sql``. No incremental SQL migration runs,
and legacy volumes are neither copied nor deleted.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "Missing dependency: install paramiko first, e.g. "
        "`python -m pip install --user paramiko -i https://pypi.tuna.tsinghua.edu.cn/simple`"
    ) from exc

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class DeployConfig:
    host: str
    user: str
    port: int
    password: Optional[str]
    key_path: Optional[str]
    deploy_dir: str
    repo_url: str
    repo_branch: str
    compose_file: str
    deepseek_api_key: Optional[str]
    mysql_root_password: Optional[str]
    mysql_database: str
    mysql_user: str
    mysql_password: Optional[str]
    redis_password: Optional[str]
    neo4j_auth: Optional[str]
    minio_root_user: str
    minio_root_password: Optional[str]
    workflow_internal_token: Optional[str]
    workflow_postgres_password: Optional[str]


BOOTSTRAP_REQUIRED_KEYS = (
    "DEEPSEEK_API_KEY",
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_DATABASE",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "REDIS_PASSWORD",
    "NEO4J_AUTH",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "WORKFLOW_INTERNAL_TOKEN",
    "WORKFLOW_POSTGRES_PASSWORD",
)

# Only these local values may update an existing remote .env. Connection and
# deploy-only variables (ALIYUN_PASSWORD, key paths, repository URLs, etc.) are
# deliberately absent so they can never leak into a container env file.
REMOTE_ENV_OVERRIDE_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_QUALITY_MODEL",
    "LLM_MAX_CONCURRENT",
    "LLM_HTTP_MAX_CONNECTIONS",
    "LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS",
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_DATABASE",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "REDIS_PASSWORD",
    "NEO4J_AUTH",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "WORKFLOW_INTERNAL_TOKEN",
    "WORKFLOW_POSTGRES_PASSWORD",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "GITHUB_TOKEN",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
)


def main() -> None:
    load_local_env()
    config = load_config()
    print(f"Connecting to {config.user}@{config.host}:{config.port} ...")
    ssh = connect(config)
    try:
        run(ssh, "uname -a", timeout=30)
        ensure_docker(ssh)
        configure_docker_mirror(ssh)
        sync_repository(ssh, config)
        upload_env(ssh, config)
        compose_up(ssh, config)
        wait_health(ssh, config)
        print()
        print("===================================================")
        print(f"  MVP deployed.  Open:  http://{config.host}")
        print("===================================================")
    finally:
        ssh.close()


def load_local_env() -> None:
    path = ".deploy.local.env"
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def load_config() -> DeployConfig:
    password = os.getenv("ALIYUN_PASSWORD")
    key_path = os.getenv("ALIYUN_KEY_PATH")
    if not password and not key_path:
        raise SystemExit("Set ALIYUN_PASSWORD or ALIYUN_KEY_PATH before deployment.")
    return DeployConfig(
        host=require_env("ALIYUN_HOST"),
        user=os.getenv("ALIYUN_USER", "root"),
        port=int(os.getenv("ALIYUN_PORT", "22")),
        password=password,
        key_path=key_path,
        deploy_dir=os.getenv("DEPLOY_DIR", "/opt/ai-resume-agent-platform"),
        repo_url=os.getenv("REPO_URL", "https://github.com/iTomGeller/ResumAI-Agent.git"),
        repo_branch=os.getenv("REPO_BRANCH", "main"),
        compose_file=os.getenv("COMPOSE_FILE", "docker-compose.prod.yml"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        mysql_root_password=os.getenv("MYSQL_ROOT_PASSWORD"),
        mysql_database=os.getenv("MYSQL_DATABASE", "resumai_agent"),
        mysql_user=os.getenv("MYSQL_USER", "resumai"),
        mysql_password=os.getenv("MYSQL_PASSWORD"),
        redis_password=os.getenv("REDIS_PASSWORD"),
        neo4j_auth=os.getenv("NEO4J_AUTH"),
        minio_root_user=os.getenv("MINIO_ROOT_USER", "resumai"),
        minio_root_password=os.getenv("MINIO_ROOT_PASSWORD"),
        workflow_internal_token=os.getenv("WORKFLOW_INTERNAL_TOKEN"),
        workflow_postgres_password=os.getenv("WORKFLOW_POSTGRES_PASSWORD"),
    )


def connect(config: DeployConfig) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, object] = {
        "hostname": config.host,
        "port": config.port,
        "username": config.user,
        "timeout": 30,
        "banner_timeout": 30,
        "auth_timeout": 30,
    }
    if config.key_path:
        kwargs["key_filename"] = config.key_path
    else:
        kwargs["password"] = config.password
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    ssh.connect(**kwargs)
    return ssh


def ensure_docker(ssh: paramiko.SSHClient) -> None:
    print("[step] Ensure Docker engine + compose plugin")
    run(
        ssh,
        "if ! command -v docker >/dev/null 2>&1; then "
        "curl -fsSL https://get.docker.com | sh; "
        "fi",
        timeout=900,
    )
    run(ssh, "systemctl enable --now docker || service docker start || true", timeout=120, check=False)
    run(ssh, "docker version", timeout=60)
    compose_ok = run(
        ssh,
        "docker compose version >/dev/null 2>&1 && echo OK || echo MISSING",
        timeout=30,
    ).strip()
    if not compose_ok.endswith("OK"):
        run(
            ssh,
            "(apt-get update && apt-get install -y docker-compose-plugin) || "
            "(yum install -y docker-compose-plugin) || true",
            timeout=600,
            check=False,
        )
    run(ssh, "docker compose version", timeout=60)


def configure_docker_mirror(ssh: paramiko.SSHClient) -> None:
    print("[step] Configure Docker China registry mirror")
    daemon_json = (
        "{\n"
        '  "registry-mirrors": [\n'
        '    "https://docker.m.daocloud.io",\n'
        '    "https://dockerproxy.com",\n'
        '    "https://docker.nju.edu.cn"\n'
        "  ],\n"
        '  "max-concurrent-downloads": 6\n'
        "}\n"
    )
    write_remote_file(ssh, "/etc/docker/daemon.json", daemon_json, mode=0o644)
    run(ssh, "systemctl restart docker || service docker restart || true", timeout=120, check=False)
    time.sleep(3)
    run(ssh, "docker info | grep -A4 'Registry Mirrors' || true", check=False)


def sync_repository(ssh: paramiko.SSHClient, config: DeployConfig) -> None:
    print("[step] Sync repository on ECS")
    run(
        ssh,
        "if ! command -v git >/dev/null 2>&1; then "
        "apt-get update && apt-get install -y git || yum install -y git; "
        "fi",
        timeout=600,
    )
    run(ssh, f"mkdir -p {config.deploy_dir}")
    run(
        ssh,
        f"if [ -d {config.deploy_dir}/.git ]; then "
        f"  cd {config.deploy_dir} && "
        f"  git remote set-url origin {config.repo_url} && "
        f"  git fetch origin {config.repo_branch} && "
        f"  git reset --hard origin/{config.repo_branch} && "
        f"  git clean -fd; "
        f"else "
        f"  rm -rf {config.deploy_dir}/* {config.deploy_dir}/.[!.]* {config.deploy_dir}/..?* 2>/dev/null; "
        f"  git clone --branch {config.repo_branch} {config.repo_url} {config.deploy_dir}; "
        f"fi",
        timeout=900,
    )
    run(ssh, f"cd {config.deploy_dir} && git log -1 --oneline", timeout=30)


def read_remote_file(ssh: paramiko.SSHClient, remote_path: str) -> Optional[str]:
    """Read a remote file without ever writing its contents to stdout."""
    sftp = ssh.open_sftp()
    try:
        try:
            with sftp.file(remote_path, "r") as remote_file:
                payload = remote_file.read()
        except IOError:
            return None
    finally:
        sftp.close()
    return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)


def parse_env_text(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def merge_env_text(content: str, updates: dict[str, str]) -> str:
    """Merge selected keys while preserving comments and untouched secrets."""
    rendered: list[str] = []
    written: set[str] = set()
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in updates:
            if key not in written:
                rendered.append(f"{key}={updates[key]}")
                written.add(key)
            continue
        rendered.append(raw_line)
    for key, value in updates.items():
        if key not in written:
            rendered.append(f"{key}={value}")
    return "\n".join(rendered).rstrip() + "\n"


def upload_env(ssh: paramiko.SSHClient, config: DeployConfig) -> None:
    remote_path = f"{config.deploy_dir}/.env"
    existing = read_remote_file(ssh, remote_path)
    existing_values = parse_env_text(existing or "")
    explicit_overrides = {
        key: value
        for key in REMOTE_ENV_OVERRIDE_KEYS
        if (value := os.getenv(key)) is not None and value.strip()
    }

    values_to_write: dict[str, str] = dict(explicit_overrides)
    if existing is None:
        print("[step] Bootstrap remote .env (remote file does not exist)")
        bootstrap_values = {
            "DEEPSEEK_API_KEY": config.deepseek_api_key or "",
            "MYSQL_ROOT_PASSWORD": config.mysql_root_password or "",
            "MYSQL_DATABASE": config.mysql_database,
            "MYSQL_USER": config.mysql_user,
            "MYSQL_PASSWORD": config.mysql_password or "",
            "REDIS_PASSWORD": config.redis_password or "",
            "NEO4J_AUTH": config.neo4j_auth or "",
            "MINIO_ROOT_USER": config.minio_root_user,
            "MINIO_ROOT_PASSWORD": config.minio_root_password or "",
            "WORKFLOW_INTERNAL_TOKEN": config.workflow_internal_token or "",
            "WORKFLOW_POSTGRES_PASSWORD": config.workflow_postgres_password or "",
        }
        existing_values.update(bootstrap_values)
        values_to_write.update(bootstrap_values)
    else:
        print(f"[step] Preserve remote .env and merge {len(explicit_overrides)} explicit non-empty overrides")

    existing_values.update(explicit_overrides)
    defaults = {
        "SPRING_PROFILES_ACTIVE": "prod",
        "BACKEND_PORT": "8080",
        "FRONTEND_PORT": "80",
        "UPLOAD_DIR": "/opt/ai-resume-agent-platform/uploads",
        "DEEPSEEK_API_URL": "https://api.deepseek.com/chat/completions",
        "DEEPSEEK_MODEL": "deepseek-v4-flash",
        "DEEPSEEK_QUALITY_MODEL": "deepseek-v4-pro",
        "LLM_MAX_CONCURRENT": "16",
        "LLM_HTTP_MAX_CONNECTIONS": "16",
        "LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS": "16",
        "MYSQL_HOST": "mysql",
        "MYSQL_PORT": "3306",
        "REDIS_HOST": "redis",
        "REDIS_PORT": "6379",
        "NEO4J_URI": "bolt://neo4j:7687",
        "MILVUS_HOST": "milvus",
        "MILVUS_PORT": "19530",
        "MILVUS_COLLECTION": "resume_chunk",
        "MILVUS_DIMENSION": "1024",
        "LANGGRAPH_RUNTIME_ENABLED": "true",
        "PUBLIC_HOST": config.host,
    }
    for key, value in defaults.items():
        if not existing_values.get(key, "").strip():
            existing_values[key] = value
            values_to_write[key] = value
    # PUBLIC_HOST belongs to this target, not to a previous ECS deployment.
    existing_values["PUBLIC_HOST"] = config.host
    values_to_write["PUBLIC_HOST"] = config.host

    neo4j_user, separator, neo4j_password = existing_values.get("NEO4J_AUTH", "").partition("/")
    if separator:
        existing_values["NEO4J_USERNAME"] = neo4j_user
        existing_values["NEO4J_PASSWORD"] = neo4j_password
        if existing is None or "NEO4J_AUTH" in explicit_overrides or not parse_env_text(existing).get("NEO4J_USERNAME"):
            values_to_write["NEO4J_USERNAME"] = neo4j_user
        if existing is None or "NEO4J_AUTH" in explicit_overrides or not parse_env_text(existing).get("NEO4J_PASSWORD"):
            values_to_write["NEO4J_PASSWORD"] = neo4j_password

    missing = [key for key in BOOTSTRAP_REQUIRED_KEYS if not existing_values.get(key, "").strip()]
    if missing:
        mode = "first bootstrap" if existing is None else "remote .env validation"
        raise SystemExit(f"Missing required keys for {mode}: {', '.join(missing)}")
    checkpoint_password = existing_values["WORKFLOW_POSTGRES_PASSWORD"]
    if not all(ch.isalnum() or ch in "._~-" for ch in checkpoint_password):
        raise SystemExit("WORKFLOW_POSTGRES_PASSWORD must be URL-safe for the checkpoint DSN")
    for key, value in existing_values.items():
        if "\r" in value or "\n" in value:
            raise SystemExit(f"Remote env value contains a newline: {key}")

    env_content = merge_env_text(existing or "", values_to_write)
    write_remote_file(ssh, remote_path, env_content, mode=0o600)
    print("[env] remote .env validated and written without logging values")


def compose_up(ssh: paramiko.SSHClient, config: DeployConfig) -> None:
    print(f"[step] docker compose up -d --build (file={config.compose_file})")
    run(
        ssh,
        f"cd {config.deploy_dir} && docker compose -f {config.compose_file} config >/dev/null",
        timeout=120,
    )
    run(
        ssh,
        f"cd {config.deploy_dir} && docker compose -f {config.compose_file} pull --ignore-pull-failures || true",
        timeout=1800,
        check=False,
    )
    run(
        ssh,
        f"cd {config.deploy_dir} && docker compose -f {config.compose_file} up -d mysql langgraph-postgres",
        timeout=600,
    )
    run(
        ssh,
        "for i in $(seq 1 60); do "
        "[ \"$(docker inspect -f '{{.State.Health.Status}}' resumai-mysql 2>/dev/null)\" = healthy ] "
        "&& exit 0; sleep 2; done; exit 1",
        timeout=150,
    )
    run(
        ssh,
        f"cd {config.deploy_dir} && docker compose -f {config.compose_file} up -d --build",
        timeout=2400,
    )
    run(ssh, f"cd {config.deploy_dir} && docker compose -f {config.compose_file} ps", timeout=60)


def wait_health(ssh: paramiko.SSHClient, config: DeployConfig) -> None:
    print("[step] Wait for /api/health on backend container, then on http://host/api/health")
    backend_ok = False
    for attempt in range(60):
        result = run(
            ssh,
            "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health "
            ">/tmp/resumai-backend.log 2>&1; echo EXIT=$?",
            timeout=30,
            check=False,
        )
        if "EXIT=0" in result:
            backend_ok = True
            print(f"  backend healthy after {attempt + 1} polls")
            break
        time.sleep(5)
    if not backend_ok:
        run(
            ssh,
            f"docker compose -f {config.deploy_dir}/{config.compose_file} logs --tail=120 ai-resume-backend || true",
            check=False,
        )
        raise SystemExit("Backend /api/health did not become healthy in time.")

    for attempt in range(20):
        result = run(
            ssh,
            "curl -fsS http://127.0.0.1/api/health >/tmp/resumai-public.log 2>&1; echo EXIT=$?",
            timeout=30,
            check=False,
        )
        if "EXIT=0" in result:
            print(f"  public /api/health healthy after {attempt + 1} polls")
            return
        time.sleep(3)
    run(
        ssh,
        f"docker compose -f {config.deploy_dir}/{config.compose_file} logs --tail=120 ai-resume-frontend || true",
        check=False,
    )
    raise SystemExit("Public /api/health (through nginx) failed.")


def write_remote_file(ssh: paramiko.SSHClient, remote_path: str, content: str, mode: int) -> None:
    sftp = ssh.open_sftp()
    try:
        remote_dir = os.path.dirname(remote_path)
        if remote_dir:
            try:
                sftp.stat(remote_dir)
            except IOError:
                run(ssh, f"mkdir -p {remote_dir}")
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(content)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def run(ssh: paramiko.SSHClient, command: str, timeout: int = 300, check: bool = True) -> str:
    print(f"$ {command}")
    stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    del stdin
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    if out:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if err:
        sys.stderr.write(err)
        if not err.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    if check and status != 0:
        raise SystemExit(f"Command failed ({status}): {command}")
    return out + err


if __name__ == "__main__":
    main()
