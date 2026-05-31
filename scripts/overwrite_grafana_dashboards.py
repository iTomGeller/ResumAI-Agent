"""Force-overwrite Grafana dashboards via API (bypasses stale DB provisioning cache)."""
from __future__ import annotations

import json
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "monitoring" / "grafana" / "provisioning" / "dashboards"

UIDS = (
    "resumai-spring-boot",
    "resumai-agents",
    "resumai-capability-rag",
    "resumai-capability-infra",
    "resumai-capability-toolcalls",
)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def dashboard_payload(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("id", None)
    data.pop("version", None)
    return {
        "dashboard": data,
        "folderId": 0,
        "overwrite": True,
        "message": "ResumAI i18n force overwrite",
    }


def main() -> None:
    env = load_env()
    password = env.get("GRAFANA_PASSWORD", "admin123")
    safe_password = password.replace("'", "'\\''")

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

    for uid in UIDS:
        path = DASH_DIR / f"{uid}.json"
        if not path.exists():
            print(f"[skip] missing {path.name}")
            continue
        payload = json.dumps(dashboard_payload(path), ensure_ascii=False)
        remote_payload = f"/tmp/grafana-{uid}.json"
        sftp = ssh.open_sftp()
        with sftp.file(remote_payload, "w") as fh:
            fh.write(payload)
        sftp.close()

        # Provisioned dashboards cannot be saved through API; reload from synced JSON instead.
        delete_cmd = (
            f"docker exec resumai-grafana curl -fsS -u admin:'{safe_password}' "
            f"-X DELETE http://127.0.0.1:3000/api/dashboards/uid/{uid} || true"
        )
        ssh.exec_command(delete_cmd, timeout=30)

    _, stdout, _ = ssh.exec_command("docker restart resumai-grafana", timeout=60)
    print(stdout.read().decode("utf-8", errors="replace").strip())
    ssh.close()
    print("[ok] grafana dashboards reloaded from provisioning files")


if __name__ == "__main__":
    main()
