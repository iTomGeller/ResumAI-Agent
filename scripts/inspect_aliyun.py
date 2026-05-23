"""SSH 一次性诊断脚本：dump ECS 上 docker compose 当前状态、构建残骸和后端日志。"""
from __future__ import annotations

import os
import sys

import paramiko


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


def main() -> None:
    load_local_env()
    host = os.getenv("ALIYUN_HOST")
    if not host:
        raise SystemExit("Missing ALIYUN_HOST")
    user = os.getenv("ALIYUN_USER", "root")
    port = int(os.getenv("ALIYUN_PORT", "22"))
    password = os.getenv("ALIYUN_PASSWORD")
    if not password:
        raise SystemExit("Missing ALIYUN_PASSWORD")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, port=port, username=user, password=password,
                look_for_keys=False, allow_agent=False, timeout=30)
    deploy_dir = os.getenv("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    commands = [
        "uptime",
        f"cd {deploy_dir} && git log -1 --oneline",
        "docker ps --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}'",
        f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps 2>&1 || true",
        "docker images | head -25",
        "df -h /",
        "free -h",
        f"ls -la {deploy_dir}",
        f"ls -la {deploy_dir}/backend/src/main/resources/db/ || true",
        f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=80 ai-resume-backend 2>&1 || true",
        f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=40 mysql 2>&1 || true",
    ]
    for cmd in commands:
        sys.stdout.write(f"\n=== $ {cmd}\n")
        sys.stdout.flush()
        _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
        sys.stdout.write(stdout.read().decode('utf-8', errors='replace'))
        err = stderr.read().decode('utf-8', errors='replace')
        if err:
            sys.stdout.write(f"[stderr] {err}")
        sys.stdout.flush()
    ssh.close()


if __name__ == "__main__":
    main()
