"""Delete provisioned Grafana dashboards from DB so file provisioning reloads them."""
from __future__ import annotations

import paramiko
from pathlib import Path

UIDS = (
    "resumai-spring-boot",
    "resumai-agents",
    "resumai-capability-rag",
    "resumai-capability-infra",
    "resumai-capability-toolcalls",
)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def main() -> None:
    env = load_env()
    password = env.get("GRAFANA_PASSWORD", "admin123")
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
    safe_password = password.replace("'", "'\\''")
    for uid in UIDS:
        cmd = (
            f"docker exec resumai-grafana curl -fsS -u admin:'{safe_password}' "
            f"-X DELETE http://127.0.0.1:3000/api/dashboards/uid/{uid} "
            f"|| echo 'skip {uid}'"
        )
        _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
        print(stdout.read().decode("utf-8", errors="replace").strip())
        err = stderr.read().decode("utf-8", errors="replace").strip()
        if err:
            print(err)
    _, stdout, _ = ssh.exec_command("docker restart resumai-grafana", timeout=60)
    print(stdout.read().decode("utf-8", errors="replace").strip())
    ssh.close()
    print("[ok] grafana dashboards re-provisioned")


if __name__ == "__main__":
    main()
