"""Deploy LangGraph workflow to ECS: build, fresh-volume startup, verify.

MySQL is initialized only from the current ``schema.sql`` mounted by Compose;
this script intentionally never executes incremental migration files.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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
    if out:
        print(out[-8000:] if len(out) > 8000 else out)
    if err:
        print(err[-4000:] if len(err) > 4000 else err)
    return code, out + err


def main() -> None:
    env = load_env()
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    host = env["ALIYUN_HOST"]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        host,
        username=env.get("ALIYUN_USER", "root"),
        password=env["ALIYUN_PASSWORD"],
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    try:
        # Merge WORKFLOW_INTERNAL_TOKEN into .env if missing
        token = env.get("WORKFLOW_INTERNAL_TOKEN")
        if not token:
            raise SystemExit("WORKFLOW_INTERNAL_TOKEN is required")
        run(
            ssh,
            f"grep -q '^WORKFLOW_INTERNAL_TOKEN=' {deploy_dir}/.env || echo 'WORKFLOW_INTERNAL_TOKEN={token}' >> {deploy_dir}/.env",
            timeout=30,
        )
        wpg = env.get("WORKFLOW_POSTGRES_PASSWORD")
        if not wpg:
            raise SystemExit("WORKFLOW_POSTGRES_PASSWORD is required")
        if not all(ch.isalnum() or ch in "._~-" for ch in wpg):
            raise SystemExit("WORKFLOW_POSTGRES_PASSWORD must be URL-safe for the checkpoint DSN")
        run(
            ssh,
            f"grep -q '^WORKFLOW_POSTGRES_PASSWORD=' {deploy_dir}/.env || echo 'WORKFLOW_POSTGRES_PASSWORD={wpg}' >> {deploy_dir}/.env",
            timeout=30,
        )
        grafana_password = env.get("GRAFANA_PASSWORD")
        if not grafana_password:
            raise SystemExit("GRAFANA_PASSWORD is required by docker-compose.prod.yml")
        run(
            ssh,
            f"grep -q '^GRAFANA_PASSWORD=' {deploy_dir}/.env || echo 'GRAFANA_PASSWORD={grafana_password}' >> {deploy_dir}/.env",
            timeout=30,
        )

        code, _ = run(
            ssh,
            f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml build ai-resume-workflow ai-resume-backend ai-resume-frontend 2>&1",
            timeout=3600,
        )
        if code != 0:
            raise SystemExit(f"docker build failed ({code})")

        code, _ = run(
            ssh,
            f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d mysql ai-resume-workflow-postgres 2>&1",
            timeout=600,
        )
        if code != 0:
            raise SystemExit(f"database startup failed ({code})")

        code, _ = run(
            ssh,
            f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d "
            "ai-resume-workflow-postgres ai-resume-workflow ai-resume-backend ai-resume-frontend 2>&1",
            timeout=600,
        )
        if code != 0:
            raise SystemExit(f"docker up failed ({code})")

        run(ssh, "free -m && df -h / && docker system df", timeout=60)
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml config >/dev/null", timeout=120)
        run(ssh, "docker stats --no-stream", timeout=60)
        code, prom_out = run(
            ssh,
            "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/actuator/prometheus | grep resumai_workflow_ | head",
            timeout=60,
        )
        if code != 0:
            print("[warn] resumai_workflow_* metrics not yet visible")
        else:
            print(prom_out[:500])

        for attempt in range(1, 31):
            code, out = run(ssh, "docker exec ai-resume-workflow curl -fsS http://127.0.0.1:8090/health", timeout=30)
            print(f"[workflow-health] {attempt}: {out.strip()[:120]}")
            if "UP" in out:
                break
            time.sleep(6)
        else:
            raise SystemExit("workflow health check failed")

        for attempt in range(1, 31):
            code, out = run(ssh, "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health", timeout=30)
            print(f"[backend-health] {attempt}: {out.strip()[:120]}")
            if "UP" in out:
                break
            time.sleep(6)
        else:
            raise SystemExit("backend health check failed")

        url = f"http://{host}/api/health"
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"[public] {resp.status} {body[:200]}")

        print("\n[ok] LangGraph workflow deployed on ECS")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
