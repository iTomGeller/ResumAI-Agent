"""把本地修改后的 docker-compose.prod.yml sftp 推到 ECS 并 up -d backend/frontend。"""
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


def fresh_ssh(retries: int = 6, wait: float = 5.0) -> paramiko.SSHClient:
    last = None
    for i in range(retries):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=os.environ["ALIYUN_HOST"],
                port=int(os.environ.get("ALIYUN_PORT", "22")),
                username=os.environ.get("ALIYUN_USER", "root"),
                password=os.environ["ALIYUN_PASSWORD"],
                look_for_keys=False, allow_agent=False,
                timeout=20, banner_timeout=30, auth_timeout=30,
            )
            return ssh
        except Exception as exc:
            last = exc
            sys.stderr.write(f"[retry] ssh connect failed: {exc}\n")
            time.sleep(wait)
    raise SystemExit(f"ssh unreachable: {last}")


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
    ssh = fresh_ssh()
    try:
        sftp = ssh.open_sftp()
        sftp.put("docker-compose.prod.yml", f"{deploy_dir}/docker-compose.prod.yml")
        sftp.close()
    finally:
        ssh.close()
    run_once(f"head -5 {deploy_dir}/docker-compose.prod.yml")
    run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -n 80",
             timeout=600)
    run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", check=False)
    ok_back = False
    for attempt in range(40):
        r = run_once(
            "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health "
            ">/tmp/back.log 2>&1; echo EXIT=$?",
            timeout=20, check=False,
        )
        if "EXIT=0" in r:
            print(f"[ok] backend healthy after {attempt + 1} polls")
            ok_back = True
            break
        time.sleep(10)
    if not ok_back:
        run_once(f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=120 ai-resume-backend",
                 check=False)
        raise SystemExit("backend never became healthy")
    ok_pub = False
    for attempt in range(20):
        r = run_once("curl -fsS http://127.0.0.1/api/health >/tmp/pub.log 2>&1; echo EXIT=$?",
                     timeout=20, check=False)
        if "EXIT=0" in r:
            print(f"[ok] public /api/health healthy after {attempt + 1} polls")
            ok_pub = True
            break
        time.sleep(5)
    if not ok_pub:
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
