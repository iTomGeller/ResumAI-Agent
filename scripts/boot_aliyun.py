"""开 swap 并启动完整 PRD compose 栈，避免大日志被 paramiko 截断。"""
from __future__ import annotations

import os
import sys
import time

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


def run(ssh: paramiko.SSHClient, command: str, timeout: int = 600, check: bool = True) -> str:
    sys.stdout.write(f"\n$ {command}\n")
    sys.stdout.flush()
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    if check and rc != 0:
        raise SystemExit(f"command failed ({rc}): {command}")
    return out + err


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
    try:
        # 1. 4GB swap，持久化到 fstab。
        run(ssh, "swapon --show || true", check=False)
        run(ssh, "if [ ! -f /swapfile ]; then "
                 "fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && "
                 "grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; "
                 "fi", timeout=180, check=False)
        run(ssh, "free -h", check=False)
        # 2. 同步最新 main，避免镜像与代码错位。
        run(ssh, f"cd {deploy_dir} && git fetch origin main && git reset --hard origin/main", timeout=120)
        # 3. 启动完整 prod 栈；先把后端镜像直接拉/构建（已构建过会秒过），再 up -d。
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml build ai-resume-backend ai-resume-frontend 2>&1 | tail -n 30",
            timeout=2400, check=False)
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -n 60",
            timeout=600)
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", timeout=60, check=False)
        # 4. 轮询 backend 健康检查。
        for attempt in range(80):
            r = run(ssh,
                    "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health "
                    ">/tmp/back.log 2>&1; echo EXIT=$?",
                    timeout=15, check=False)
            if "EXIT=0" in r:
                print(f"[ok] backend healthy after {attempt + 1} polls")
                break
            time.sleep(6)
        else:
            run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=80 ai-resume-backend",
                check=False)
            run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=40 mysql", check=False)
            raise SystemExit("backend never became healthy")
        # 5. 公网链路通断。
        for attempt in range(30):
            r = run(ssh, "curl -fsS http://127.0.0.1/api/health >/tmp/pub.log 2>&1; echo EXIT=$?",
                    timeout=15, check=False)
            if "EXIT=0" in r:
                print(f"[ok] public /api/health healthy after {attempt + 1} polls")
                break
            time.sleep(5)
        else:
            run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=80 ai-resume-frontend",
                check=False)
            raise SystemExit("public /api/health never became healthy")
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", check=False)
        print()
        print("===================================================")
        print(f"  Full PRD stack deployed.  Open: http://{host}")
        print("===================================================")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
