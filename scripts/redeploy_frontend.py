"""Upload frontend sources and force rebuild on ECS."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_local_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_local_env()
    host = os.environ.get("ALIYUN_HOST")
    password = os.environ.get("ALIYUN_PASSWORD")
    if not host:
        raise SystemExit("Missing ALIYUN_HOST")
    if not password:
        raise SystemExit("Missing ALIYUN_PASSWORD")
    root = Path(__file__).resolve().parents[1]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        host,
        username="root",
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
    )
    try:
        sftp = ssh.open_sftp()
        for name in ("App.vue", "style.css"):
            local = root / "frontend" / "src" / name
            remote = f"/opt/ai-resume-agent-platform/frontend/src/{name}"
            sftp.put(str(local), remote)
            print(f"uploaded {remote}")
        sftp.close()

        commands = [
            "grep -n sub-tab-bar /opt/ai-resume-agent-platform/frontend/src/App.vue | head -3",
            "cd /opt/ai-resume-agent-platform && "
            "docker compose -f docker-compose.prod.yml build --no-cache ai-resume-frontend 2>&1 | tail -n 35",
            "cd /opt/ai-resume-agent-platform && "
            "docker compose -f docker-compose.prod.yml up -d ai-resume-frontend 2>&1 | tail -n 8",
            "docker exec ai-resume-frontend ls /usr/share/nginx/html/assets/ | head -5",
        ]
        for command in commands:
            print(f"\n$ {command}")
            _, stdout, stderr = ssh.exec_command(command, timeout=1800)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            if out:
                print(out)
            if err:
                print(err)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
