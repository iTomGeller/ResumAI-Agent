"""Start ResumAI on ECS with mirror-aware pulls and fresh schema init.

The current schema is loaded by MySQL's entrypoint into the versioned named
volume.  Existing/legacy volumes are never copied, migrated, or deleted.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEPLOY_DIR = "/opt/ai-resume-agent-platform"

PUBLIC_IMAGES = [
    "docker.m.daocloud.io/library/mysql:8.0",
    "docker.m.daocloud.io/library/redis:7.2-alpine",
    "minio/minio:RELEASE.2024-05-28T17-19-04Z",
    "registry.cn-hangzhou.aliyuncs.com/google_containers/etcd:3.5.15-0",
    "milvusdb/milvus:v2.4.4",
    "docker.m.daocloud.io/prom/prometheus:v2.53.0",
    "docker.m.daocloud.io/grafana/grafana:11.1.0",
]

CORE_SERVICES = (
    "mysql redis minio etcd milvus "
    "ai-resume-workflow-postgres ai-resume-workflow "
    "ai-resume-backend ai-resume-frontend prometheus grafana"
)

NAMED_VOLUMES = [
    "resumai-mysql-data-conversation-v1",
    "resumai-redis-data",
    "resumai-minio-data",
    "resumai-etcd-data",
    "resumai-milvus-data",
    "resumai-uploads-data",
    "resumai-backend-logs",
    # Neo4j is optional for the core demo path, but creating these keeps compose config valid.
    "resumai-neo4j-data",
    "resumai-neo4j-logs",
    "resumai-neo4j-plugins",
    "resumai-workflow-postgres-data",
    "resumai-prometheus-data",
    "resumai-grafana-data",
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 1800) -> tuple[int, str]:
    print(f"\n$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    text = out + err
    if text:
        print(text[-6000:] if len(text) > 6000 else text)
    return code, text


def must(ssh: paramiko.SSHClient, cmd: str, timeout: int = 1800) -> str:
    code, text = run(ssh, cmd, timeout)
    if code != 0:
        raise SystemExit(f"command failed ({code}): {cmd}")
    return text


def pull_with_retry(ssh: paramiko.SSHClient, image: str, attempts: int = 4) -> None:
    for attempt in range(1, attempts + 1):
        code, _ = run(ssh, f"timeout 900 docker pull {image}", timeout=930)
        if code == 0:
            return
        print(f"[pull] {image} failed attempt={attempt}/{attempts}")
        run(ssh, "pkill -f 'docker pull' || true", timeout=20)
        time.sleep(8)
    raise SystemExit(f"docker pull failed after retries: {image}")


def main() -> None:
    env = load_env()
    root = Path(__file__).resolve().parents[1]
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
        sftp = ssh.open_sftp()
        sftp.put(str(root / "docker-compose.prod.yml"), f"{DEPLOY_DIR}/docker-compose.prod.yml")
        sftp.close()
        print("[sync] uploaded docker-compose.prod.yml")

        must(ssh, "mkdir -p /etc/docker", timeout=30)
        daemon = (
            "'{\"registry-mirrors\":[\"https://docker.m.daocloud.io\","
            "\"https://mirror.ccs.tencentyun.com\",\"https://hub-mirror.c.163.com\"],"
            "\"max-concurrent-downloads\":3}'"
        )
        must(ssh, f"printf %s {daemon} > /etc/docker/daemon.json && systemctl restart docker", timeout=60)
        run(ssh, "pkill -f 'docker pull' || true", timeout=20)
        must(ssh, "free -h && df -h / && docker system df", timeout=60)

        for image in PUBLIC_IMAGES:
            pull_with_retry(ssh, image)

        for volume in NAMED_VOLUMES:
            must(ssh, f"docker volume inspect {volume} >/dev/null 2>&1 || docker volume create {volume}", timeout=30)

        must(ssh, f"cd {DEPLOY_DIR} && docker compose -f docker-compose.prod.yml config >/dev/null", timeout=120)
        must(
            ssh,
            f"cd {DEPLOY_DIR} && docker compose -f docker-compose.prod.yml up -d --no-build {CORE_SERVICES} 2>&1",
            timeout=1800,
        )

        for attempt in range(1, 60):
            code, out = run(ssh, "curl -fsS http://127.0.0.1/api/health", timeout=20)
            if code == 0 and "UP" in out:
                print(f"[ok] health UP attempt={attempt}")
                break
            time.sleep(10)
        else:
            run(ssh, f"cd {DEPLOY_DIR} && docker compose -f docker-compose.prod.yml ps", timeout=30)
            raise SystemExit("health check failed")

        run(ssh, f"cd {DEPLOY_DIR} && docker compose -f docker-compose.prod.yml ps", timeout=30)
        must(ssh, "docker stats --no-stream", timeout=60)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
