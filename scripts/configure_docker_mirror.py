"""Configure Docker registry mirror on Aliyun ECS."""
from __future__ import annotations

import json
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
    daemon = {
        "registry-mirrors": [
            "https://docker.m.daocloud.io",
            "https://mirror.ccs.tencentyun.com",
            "https://hub-mirror.c.163.com",
        ],
        "max-concurrent-downloads": 10,
    }
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"), password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)
    try:
        sftp = ssh.open_sftp()
        with sftp.open("/etc/docker/daemon.json", "w") as f:
            f.write(json.dumps(daemon, indent=2))
        sftp.close()
        _, o, e = ssh.exec_command("systemctl restart docker && sleep 3 && docker info | grep -A5 'Registry Mirrors'", timeout=60)
        print(o.read().decode())
        print(e.read().decode())
        print("[ok] docker mirror configured")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
