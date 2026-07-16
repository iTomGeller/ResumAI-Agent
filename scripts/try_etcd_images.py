"""Try etcd image candidates on ECS and print the first usable one."""
from __future__ import annotations

from pathlib import Path

import paramiko

CANDIDATES = [
    "registry.cn-hangzhou.aliyuncs.com/google_containers/etcd:3.5.15-0",
    "registry.aliyuncs.com/google_containers/etcd:3.5.15-0",
    "registry.cn-hangzhou.aliyuncs.com/acs/etcd:3.5.15-0",
    "quay.io/coreos/etcd:v3.5.15",
    "bitnami/etcd:3.5.15",
]


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
        for image in CANDIDATES:
            print(f"\n=== trying {image} ===")
            _, stdout, stderr = ssh.exec_command(f"timeout 300 docker pull {image}", timeout=330)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            print((out + err)[-4000:])
            code = stdout.channel.recv_exit_status()
            if code == 0:
                print(f"\n[ok] ETCD_IMAGE={image}")
                return
        raise SystemExit("no etcd image candidate pulled successfully")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
