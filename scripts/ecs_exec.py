"""Run one shell command (or upload/download a file) on the deployment ECS.

Credentials come from environment variables or `.deploy.local.env`
(ALIYUN_HOST / ALIYUN_USER / ALIYUN_PASSWORD or ALIYUN_KEY_PATH).
No secret is ever stored in this file.

Usage:
  python scripts/ecs_exec.py "docker ps -a"
  python scripts/ecs_exec.py --timeout 1800 "cd /opt/app && mvn -q test"
  python scripts/ecs_exec.py --upload local.txt /tmp/remote.txt
  python scripts/ecs_exec.py --download /tmp/remote.log local.log
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import paramiko
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: python -m pip install --user paramiko "
        "-i https://pypi.tuna.tsinghua.edu.cn/simple"
    ) from exc

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_local_env() -> None:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".deploy.local.env")
    if not os.path.exists(path):
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


def connect() -> paramiko.SSHClient:
    host = os.environ.get("ALIYUN_HOST")
    if not host:
        raise SystemExit("ALIYUN_HOST missing (set env or .deploy.local.env)")
    user = os.environ.get("ALIYUN_USER", "root")
    port = int(os.environ.get("ALIYUN_PORT", "22"))
    password = os.environ.get("ALIYUN_PASSWORD")
    key_path = os.environ.get("ALIYUN_KEY_PATH")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": 30,
        "banner_timeout": 30,
        "auth_timeout": 30,
    }
    if key_path:
        kwargs["key_filename"] = key_path
    else:
        if not password:
            raise SystemExit("Set ALIYUN_PASSWORD or ALIYUN_KEY_PATH")
        kwargs["password"] = password
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    last_error = None
    for attempt in range(4):
        try:
            ssh.connect(**kwargs)
            return ssh
        except Exception as exc:  # transient banner/transport errors
            last_error = exc
            time.sleep(2 + attempt * 3)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    raise SystemExit("SSH connect failed after retries: %s" % last_error)


def exec_stream(ssh: paramiko.SSHClient, command: str, timeout: int) -> int:
    transport = ssh.get_transport()
    channel = transport.open_session()
    channel.set_combine_stderr(True)
    channel.exec_command(command)
    deadline = time.time() + timeout
    buffer = b""
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65536)
            if chunk:
                buffer += chunk
                try:
                    text = buffer.decode("utf-8")
                    buffer = b""
                except UnicodeDecodeError:
                    text = buffer[:-4].decode("utf-8", errors="replace")
                    buffer = buffer[-4:]
                sys.stdout.write(text)
                sys.stdout.flush()
        elif channel.exit_status_ready():
            break
        else:
            if time.time() > deadline:
                channel.close()
                sys.stdout.write("\n[ecs_exec] TIMEOUT after %ss\n" % timeout)
                return 124
            time.sleep(0.2)
    while channel.recv_ready():
        sys.stdout.write(channel.recv(65536).decode("utf-8", errors="replace"))
    if buffer:
        sys.stdout.write(buffer.decode("utf-8", errors="replace"))
    sys.stdout.flush()
    status = channel.recv_exit_status()
    sys.stdout.write("\n[ecs_exec] exit=%d\n" % status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute command on deployment ECS")
    parser.add_argument("command", nargs="?", help="shell command to run remotely")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--script", help="local shell script to upload and run remotely")
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"))
    parser.add_argument("--download", nargs=2, metavar=("REMOTE", "LOCAL"))
    args = parser.parse_args()

    load_local_env()
    ssh = connect()
    try:
        if args.upload:
            sftp = ssh.open_sftp()
            sftp.put(args.upload[0], args.upload[1])
            sftp.close()
            print("[ecs_exec] uploaded %s -> %s" % (args.upload[0], args.upload[1]))
            return 0
        if args.download:
            sftp = ssh.open_sftp()
            sftp.get(args.download[0], args.download[1])
            sftp.close()
            print("[ecs_exec] downloaded %s -> %s" % (args.download[0], args.download[1]))
            return 0
        if args.script:
            with open(args.script, "rb") as f:
                payload = f.read().replace(b"\r\n", b"\n")
            remote_path = "/tmp/ecs_exec_%d.sh" % int(time.time() * 1000)
            sftp = ssh.open_sftp()
            with sftp.file(remote_path, "wb") as rf:
                rf.write(payload)
            sftp.close()
            try:
                return exec_stream(ssh, "bash %s" % remote_path, args.timeout)
            finally:
                try:
                    ssh.exec_command("rm -f %s" % remote_path, timeout=10)
                except Exception:
                    pass
        if not args.command:
            parser.error("command required unless --upload/--download/--script")
        return exec_stream(ssh, args.command, args.timeout)
    finally:
        ssh.close()


if __name__ == "__main__":
    sys.exit(main())
