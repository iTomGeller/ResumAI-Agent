"""
Deploy ResumAI-Agent (full PRD stack) to an Aliyun ECS via SSH.

Required environment variables (or values in `.deploy.local.env`):
  ALIYUN_HOST
  ALIYUN_PASSWORD or ALIYUN_KEY_PATH
  DEEPSEEK_API_KEY
  MYSQL_ROOT_PASSWORD
  MYSQL_PASSWORD
  REDIS_PASSWORD
  NEO4J_AUTH
  MINIO_ROOT_PASSWORD

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
The default deployment now launches the **complete PRD stack**:
backend, frontend, MySQL, Redis, Neo4j, Milvus (etcd + minio).
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
    deepseek_api_key: str
    mysql_root_password: str
    mysql_database: str
    mysql_user: str
    mysql_password: str
    redis_password: str
    neo4j_auth: str
    minio_root_user: str
    minio_root_password: str


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
    deepseek_api_key = require_env("DEEPSEEK_API_KEY")
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
        deepseek_api_key=deepseek_api_key,
        mysql_root_password=require_env("MYSQL_ROOT_PASSWORD"),
        mysql_database=os.getenv("MYSQL_DATABASE", "resumai_agent"),
        mysql_user=os.getenv("MYSQL_USER", "resumai"),
        mysql_password=require_env("MYSQL_PASSWORD"),
        redis_password=require_env("REDIS_PASSWORD"),
        neo4j_auth=require_env("NEO4J_AUTH"),
        minio_root_user=os.getenv("MINIO_ROOT_USER", "resumai"),
        minio_root_password=require_env("MINIO_ROOT_PASSWORD"),
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


def upload_env(ssh: paramiko.SSHClient, config: DeployConfig) -> None:
    print("[step] Upload .env to ECS (full PRD stack)")
    neo4j_user, _, neo4j_pass = config.neo4j_auth.partition("/")
    env_content = "\n".join(
        [
            "SPRING_PROFILES_ACTIVE=prod",
            "BACKEND_PORT=8080",
            "FRONTEND_PORT=80",
            "UPLOAD_DIR=/opt/ai-resume-agent-platform/uploads",
            f"DEEPSEEK_API_KEY={config.deepseek_api_key}",
            "DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions",
            "DEEPSEEK_MODEL=deepseek-chat",
            "LANGSMITH_API_KEY=",
            "OTEL_EXPORTER_OTLP_ENDPOINT=",
            "MYSQL_HOST=mysql",
            "MYSQL_PORT=3306",
            f"MYSQL_ROOT_PASSWORD={config.mysql_root_password}",
            f"MYSQL_DATABASE={config.mysql_database}",
            f"MYSQL_USER={config.mysql_user}",
            f"MYSQL_PASSWORD={config.mysql_password}",
            "REDIS_HOST=redis",
            "REDIS_PORT=6379",
            f"REDIS_PASSWORD={config.redis_password}",
            "NEO4J_URI=bolt://neo4j:7687",
            f"NEO4J_USERNAME={neo4j_user or 'neo4j'}",
            f"NEO4J_PASSWORD={neo4j_pass or neo4j_user}",
            f"NEO4J_AUTH={config.neo4j_auth}",
            "MILVUS_HOST=milvus",
            "MILVUS_PORT=19530",
            "MILVUS_COLLECTION=resume_chunk",
            "MILVUS_DIMENSION=1024",
            f"MINIO_ROOT_USER={config.minio_root_user}",
            f"MINIO_ROOT_PASSWORD={config.minio_root_password}",
            "",
        ]
    )
    write_remote_file(ssh, f"{config.deploy_dir}/.env", env_content, mode=0o600)


def compose_up(ssh: paramiko.SSHClient, config: DeployConfig) -> None:
    print(f"[step] docker compose up -d --build (file={config.compose_file})")
    run(
        ssh,
        f"cd {config.deploy_dir} && docker compose -f {config.compose_file} pull --ignore-pull-failures || true",
        timeout=1800,
        check=False,
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
