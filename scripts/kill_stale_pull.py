"""Kill stale direct Docker Hub pulls on ECS."""
from __future__ import annotations

from pathlib import Path

import paramiko


def main() -> None:
    env: dict[str, str] = {}
    for line in Path(".deploy.local.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        env["ALIYUN_HOST"],
        username=env.get("ALIYUN_USER", "root"),
        password=env["ALIYUN_PASSWORD"],
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    try:
        cmd = (
            "pkill -f 'scripts/start_ecs_stack.py' || true; "
            "pkill -f 'docker pull' || true; "
            "pkill -f 'docker compose' || true; "
            "pgrep -af 'docker pull|docker compose|start_ecs_stack' || echo no-docker-deploy-process"
        )
        _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
        print(stdout.read().decode("utf-8", errors="replace"))
        err = stderr.read().decode("utf-8", errors="replace")
        if err:
            print(err)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
