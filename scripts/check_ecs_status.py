"""Quick ECS status check."""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    env = {}
    for line in Path(__file__).resolve().parents[1].joinpath(".deploy.local.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username="root", password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)
    cmds = [
        'docker ps -a --format "table {{.Names}}\t{{.Status}}" | sed -n "1,25p"',
        "ls -la /opt/ai-resume-agent-platform/backend/settings.xml 2>&1",
        "curl -fsS -m 5 http://127.0.0.1/api/health 2>&1 || echo health-fail",
        'docker images --format "{{.Repository}}:{{.Tag}}" | grep -E "resumai|milvus|mysql|redis|neo4j|minio|etcd" | sed -n "1,30p"',
        "pgrep -af 'docker pull|docker compose' || echo no-docker-deploy-process",
        "pgrep -af 'docker compose.*build' || echo no-build-running",
        "free -h && df -h / && docker system df",
    ]
    try:
        for cmd in cmds:
            _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
            print(f"=== {cmd} ===")
            print(stdout.read().decode("utf-8", errors="replace")[:3000])
            err = stderr.read().decode("utf-8", errors="replace")
            if err.strip():
                print("ERR:", err[:500])
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
