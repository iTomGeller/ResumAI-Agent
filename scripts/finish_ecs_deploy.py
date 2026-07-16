"""Wait for ECS docker build and bring stack up."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEPLOY_DIR = "/opt/ai-resume-agent-platform"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 3600) -> tuple[int, str]:
    print(f"\n$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    text = out + err
    if text:
        print(text[-12000:] if len(text) > 12000 else text)
    return code, text


def main() -> None:
    env = load_env()
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
        for i in range(120):
            code, out = run(ssh, "pgrep -af 'docker compose.*build' || echo idle", timeout=20)
            if "idle" in out and "docker compose" not in out.split("idle")[0]:
                print(f"[wait-build] idle after poll {i + 1}")
                break
            print(f"[wait-build] still building... poll {i + 1}")
            time.sleep(15)
        else:
            print("[warn] build wait timeout, continuing")

        code, _ = run(
            ssh,
            f"cd {DEPLOY_DIR} && docker compose -f docker-compose.prod.yml build ai-resume-backend ai-resume-frontend ai-resume-workflow 2>&1",
            timeout=3600,
        )
        if code != 0:
            raise SystemExit(f"build failed ({code})")

        code, _ = run(
            ssh,
            f"cd {DEPLOY_DIR} && docker compose -f docker-compose.prod.yml up -d 2>&1",
            timeout=1800,
        )
        if code != 0:
            raise SystemExit(f"compose up failed ({code})")

        mysql_root = env.get("MYSQL_ROOT_PASSWORD", "ResumaiRoot!2026")
        mysql_db = env.get("MYSQL_DATABASE", "resumai_agent")
        for migration in ("migration-v5-langgraph-workflow.sql", "migration-v6-trace-contract.sql"):
            run(
                ssh,
                f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} "
                f"< {DEPLOY_DIR}/backend/src/main/resources/db/{migration}",
                timeout=120,
            )

        for attempt in range(1, 41):
            code, out = run(ssh, "curl -fsS http://127.0.0.1/api/health", timeout=20)
            if code == 0 and "UP" in out:
                print(f"[ok] health UP after {attempt} polls")
                break
            time.sleep(10)
        else:
            raise SystemExit("health check failed")

        run(ssh, "docker compose -f docker-compose.prod.yml ps", timeout=30)
        print("\n[ok] ECS stack is up")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
