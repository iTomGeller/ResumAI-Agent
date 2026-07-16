"""Initialize fresh ECS: swap, sysctl, Docker."""
from __future__ import annotations

import sys
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


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 600) -> str:
    print(f"\n$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out[-4000:])
    if err:
        print(err[-2000:])
    if code != 0:
        raise SystemExit(f"failed ({code}): {cmd}")
    return out


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
        run(
            ssh,
            "test -f /swapfile || (fallocate -l 8G /swapfile && chmod 600 /swapfile && "
            "mkswap /swapfile && swapon /swapfile && "
            "grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab)",
        )
        run(ssh, "bash -lc \"echo vm.swappiness=20 > /etc/sysctl.d/99-resumai.conf\"")
        run(ssh, "bash -lc \"echo vm.max_map_count=262144 >> /etc/sysctl.d/99-resumai.conf\"")
        run(ssh, "sysctl --system")
        run(ssh, "free -h && sysctl vm.swappiness vm.max_map_count && df -h /")

        run(
            ssh,
            "command -v docker >/dev/null 2>&1 || "
            "(apt-get update -y && apt-get install -y docker.io docker-compose-v2 || "
            "apt-get install -y docker.io docker-compose-plugin)",
            timeout=1200,
        )
        run(ssh, "systemctl enable docker && systemctl start docker")
        run(ssh, "docker --version && docker compose version")
        print("\n[ok] ECS docker init complete")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
