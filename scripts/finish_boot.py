"""短会话版：milvus 现在 healthy，把 backend + frontend 拉起，并健康检查。"""
from __future__ import annotations

import os
import sys
import time

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_local_env() -> None:
    if not os.path.exists(".deploy.local.env"):
        return
    with open(".deploy.local.env", "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def fresh_ssh() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=os.environ["ALIYUN_HOST"],
        port=int(os.environ.get("ALIYUN_PORT", "22")),
        username=os.environ.get("ALIYUN_USER", "root"),
        password=os.environ["ALIYUN_PASSWORD"],
        look_for_keys=False, allow_agent=False, timeout=30, banner_timeout=30, auth_timeout=30,
    )
    return ssh


def run_once(command: str, timeout: int = 120, check: bool = True) -> str:
    sys.stdout.write(f"\n$ {command}\n"); sys.stdout.flush()
    ssh = fresh_ssh()
    try:
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if out:
            sys.stdout.write(out); sys.stdout.flush()
        if err:
            sys.stderr.write(err); sys.stderr.flush()
        if check and rc != 0:
            raise SystemExit(f"command failed ({rc}): {command}")
        return out + err
    finally:
        ssh.close()


def main() -> None:
    load_local_env()
    if not os.environ.get("ALIYUN_HOST"):
        raise SystemExit("Missing ALIYUN_HOST")
    deploy_dir = os.environ.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", check=False)
    run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -n 80",
             timeout=600)
    run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", check=False)
    backend_ok = False
    for attempt in range(60):
        r = run_once(
            "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health "
            ">/tmp/back.log 2>&1; echo EXIT=$?",
            timeout=30, check=False,
        )
        if "EXIT=0" in r:
            print(f"[ok] backend healthy after {attempt + 1} polls")
            backend_ok = True
            break
        time.sleep(8)
    if not backend_ok:
        run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=100 ai-resume-backend",
                 check=False)
        raise SystemExit("backend never became healthy")
    for attempt in range(40):
        r = run_once("curl -fsS http://127.0.0.1/api/health >/tmp/pub.log 2>&1; echo EXIT=$?",
                     timeout=20, check=False)
        if "EXIT=0" in r:
            print(f"[ok] public /api/health healthy after {attempt + 1} polls")
            break
        time.sleep(5)
    else:
        run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=80 ai-resume-frontend",
                 check=False)
        raise SystemExit("public /api/health never became healthy")
    run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", check=False)
    run_once("free -h", check=False)
    print("\n===================================================")
    print(f"  Full PRD stack live.  Open: http://{os.environ['ALIYUN_HOST']}")
    print("===================================================")


if __name__ == "__main__":
    main()
