from __future__ import annotations

import sys
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(__file__).resolve().parents[1].joinpath(".deploy.local.env").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def main() -> None:
    trace_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not trace_id:
        raise SystemExit("usage: check_meta_evolution.py <trace-id>")
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
    sql = (
        "SELECT trace_id,evolution_type,target_table,risk_level,approval_status,"
        "LEFT(reason,120) reason FROM meta_evolution_history "
        f"WHERE trace_id='{trace_id}' ORDER BY create_time DESC;"
    )
    cmd = (
        "cd /opt/ai-resume-agent-platform; "
        "MYSQL_PASSWORD=$(grep '^MYSQL_PASSWORD=' .env | cut -d= -f2-); "
        f"docker exec resumai-mysql mysql -uresumai -p\"$MYSQL_PASSWORD\" resumai_agent -e \"{sql}\""
    )
    try:
        _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
        print(stdout.read().decode("utf-8", "replace"))
        err = stderr.read().decode("utf-8", "replace")
        if err.strip():
            print(err)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
