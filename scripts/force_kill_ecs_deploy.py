"""Force kill stale Docker deploy processes on ECS."""
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
        cmd = r"""
python3 - <<'PY'
import os
import signal
import subprocess

patterns = ("docker pull", "docker compose", "start_ecs_stack.py")
out = subprocess.run(["ps", "-eo", "pid,args"], text=True, capture_output=True, check=False).stdout
for line in out.splitlines()[1:]:
    parts = line.strip().split(None, 1)
    if len(parts) != 2:
        continue
    pid_s, args = parts
    if any(p in args for p in patterns) and "force_kill_ecs_deploy.py" not in args:
        try:
            os.kill(int(pid_s), signal.SIGKILL)
            print(f"killed {pid_s} {args[:120]}")
        except ProcessLookupError:
            pass
PY
ps -eo pid,args | grep -E 'docker pull|docker compose|start_ecs_stack' | grep -v grep || echo clean
"""
        _, stdout, stderr = ssh.exec_command(cmd, timeout=60)
        print(stdout.read().decode("utf-8", errors="replace"))
        err = stderr.read().decode("utf-8", errors="replace")
        if err:
            print(err)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
