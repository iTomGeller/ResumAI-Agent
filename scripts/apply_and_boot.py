"""绕过 GitHub：本地 patch 直接 sftp 到 ECS 应用，再 docker compose 起栈。"""
from __future__ import annotations

import os
import sys
import time

import paramiko

# Windows 默认 cp936/GBK，写入 npm 的 ✓ 等字符会抛 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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
    deploy_dir = os.getenv("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    patch_local = os.getenv("PATCH_FILE", ".git/last.patch")
    if not os.path.exists(patch_local):
        raise SystemExit(f"patch file missing: {patch_local}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, port=port, username=user, password=password,
                look_for_keys=False, allow_agent=False, timeout=30)
    try:
        sftp = ssh.open_sftp()
        remote_patch = f"{deploy_dir}/.last.patch"
        sftp.put(patch_local, remote_patch)
        sftp.close()
        run(ssh, f"ls -la {remote_patch}")
        run(ssh, f"cd {deploy_dir} && git config user.email deploy@resumai.local && git config user.name deploy",
            timeout=30, check=False)
        # 已应用过的 patch 直接跳过；否则 git am 失败再 fallback 到 git apply。
        run(ssh,
            "cd " + deploy_dir + " && "
            "if git log --oneline -50 | grep -q 'Drop unused vis-network'; then "
            "  echo '[skip] patch already in history'; "
            "else "
            "  git am --abort 2>/dev/null; "
            "  git am --keep-cr " + remote_patch + " 2>&1 || "
            "  (echo '[fallback] git am failed, using git apply'; git am --abort 2>/dev/null; git apply " + remote_patch + "); "
            "fi",
            timeout=120, check=False)
        run(ssh, f"cd {deploy_dir} && git log -1 --oneline && ls frontend/ | head -10", check=False)
        # 重新构建 + 启动。
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml build ai-resume-frontend ai-resume-backend 2>&1 | tail -n 40",
            timeout=2400)
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -n 60",
            timeout=600)
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml ps", check=False)
        # 健康检查。
        ok_back = False
        for attempt in range(80):
            r = run(ssh,
                    "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health "
                    ">/tmp/back.log 2>&1; echo EXIT=$?",
                    timeout=15, check=False)
            if "EXIT=0" in r:
                print(f"[ok] backend healthy after {attempt + 1} polls")
                ok_back = True
                break
            time.sleep(8)
        if not ok_back:
            run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=80 ai-resume-backend",
                check=False)
            run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml logs --tail=40 mysql", check=False)
            raise SystemExit("backend never became healthy")
        for attempt in range(40):
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
        run(ssh, "free -h", check=False)
        print()
        print("===================================================")
        print(f"  Full PRD stack deployed.  Open: http://{host}")
        print("===================================================")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
