"""Sync local backend/frontend/monitoring sources to ECS and rebuild app containers."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    if not path.exists():
        raise SystemExit(f"Missing {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def run(ssh: paramiko.SSHClient, command: str, timeout: int = 3600, allow_fail: bool = False) -> str:
    print(f"\n$ {command}")
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if err:
        print(err)
    if code != 0 and not allow_fail:
        raise SystemExit(f"command failed ({code}): {command}")
    return out + err


def wait_health(host: str, attempts: int = 30, sleep_s: int = 6) -> None:
    url = f"http://{host}/api/health"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                print(f"[health] attempt {attempt}: {resp.status} {body[:120]}")
                if resp.status == 200 and "UP" in body:
                    return
        except Exception as exc:
            print(f"[health] attempt {attempt}: {exc}")
        time.sleep(sleep_s)
    raise SystemExit("public health check timed out after rebuild")


def main() -> None:
    env = load_env()
    root = Path(__file__).resolve().parents[1]
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    host = env["ALIYUN_HOST"]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        host,
        username=env.get("ALIYUN_USER", "root"),
        password=env["ALIYUN_PASSWORD"],
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    uploaded = 0
    try:
        sftp = ssh.open_sftp()
        uploads: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith(("backend/src/", "backend/pom.xml", "backend/skills/", "frontend/src/", "frontend/nginx.conf", "monitoring/grafana/", "docker-compose.prod.yml")) or rel == "docker-compose.langfuse.yml":
                uploads.append(path)
        for local in uploads:
            remote = f"{deploy_dir}/{local.relative_to(root).as_posix()}"
            remote_dir = os.path.dirname(remote)
            for attempt in range(3):
                try:
                    try:
                        sftp.stat(remote_dir)
                    except OSError:
                        run(ssh, f"mkdir -p {remote_dir}", timeout=30)
                    sftp.put(str(local), remote)
                    uploaded += 1
                    print(f"uploaded {local.relative_to(root).as_posix()}")
                    break
                except (OSError, paramiko.SSHException) as exc:
                    if attempt == 2:
                        raise
                    print(f"upload retry {attempt + 1} for {local.name}: {exc}")
                    try:
                        sftp.close()
                    except Exception:
                        pass
                    ssh.close()
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(
                        host,
                        username=env.get("ALIYUN_USER", "root"),
                        password=env["ALIYUN_PASSWORD"],
                        look_for_keys=False,
                        allow_agent=False,
                        timeout=30,
                    )
                    sftp = ssh.open_sftp()
        sftp.close()
        print(f"\n[sync] uploaded {uploaded} files")

        stale_files = [
            "backend/src/main/java/com/resumai/agent/ai/CompositeToolProvider.java",
            "backend/src/main/java/com/resumai/agent/ai/LocalToolExecutor.java",
            "backend/src/main/java/com/resumai/agent/ai/AgentLoopResult.java",
            "backend/src/main/java/com/resumai/agent/ai/AgentTools.java",
            "backend/src/main/java/com/resumai/agent/ai/LangfuseAgentListener.java",
        ]
        rm_cmd = " ".join(f'rm -f "{deploy_dir}/{f}"' for f in stale_files)
        run(ssh, rm_cmd, timeout=30, allow_fail=True)
        print(f"[cleanup] removed {len(stale_files)} stale files")

        merge_env_keys = [
            "DEEPSEEK_API_KEY", "DEEPSEEK_API_URL", "DEEPSEEK_MODEL",
            "EMBEDDING_PROVIDER", "EMBEDDING_ENABLED", "EMBEDDING_API_KEY",
            "EMBEDDING_BASE_URL", "EMBEDDING_MODEL",
            "MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "REDIS_PASSWORD",
            "NEO4J_AUTH", "MINIO_ROOT_PASSWORD", "GRAFANA_PASSWORD",
            "MILVUS_DIMENSION",
            "LANGFUSE_OTEL_ENDPOINT", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ]
        merge_lines = []
        for key in merge_env_keys:
            if key in env and env[key]:
                val = env[key].replace("'", "'\\''")
                merge_lines.append(f"{key}={val}")
        if merge_lines:
            merge_script = (
                f"ENV_FILE={deploy_dir}/.env; "
                "touch \"$ENV_FILE\"; "
                + " ".join(
                    f"grep -q '^{k}=' \"$ENV_FILE\" && sed -i 's|^{k}=.*|{line}|' \"$ENV_FILE\" || echo '{line}' >> \"$ENV_FILE\";"
                    for k, line in [(l.split("=", 1)[0], l) for l in merge_lines]
                )
            )
            run(ssh, merge_script, timeout=60)
            print(f"[env] merged {len(merge_lines)} keys into {deploy_dir}/.env")

        build_log = run(
            ssh,
            f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml build "
            f"ai-resume-backend ai-resume-frontend 2>&1",
            timeout=3600,
        )
        if "Successfully tagged" not in build_log and "naming to" not in build_log.lower():
            print("[warn] build log missing success marker — verify images manually")
        if "ERROR" in build_log or "failed to solve" in build_log.lower():
            raise SystemExit("docker build reported errors")

        run(
            ssh,
            f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d "
            f"ai-resume-backend ai-resume-frontend grafana prometheus 2>&1",
            timeout=600,
        )

        langfuse_compose = root / "docker-compose.langfuse.yml"
        if langfuse_compose.exists():
            run(
                ssh,
                f"cd {deploy_dir} && docker compose -f docker-compose.langfuse.yml up -d 2>&1",
                timeout=600,
                allow_fail=True,
            )
            print("[langfuse] started langfuse stack")

        local_migration_v4 = root / "backend/src/main/resources/db/migration-v4.sql"
        if local_migration_v4.exists():
            remote_v4 = f"{deploy_dir}/backend/src/main/resources/db/migration-v4.sql"
            env_text = run(ssh, f"grep -E '^MYSQL_(ROOT_PASSWORD|DATABASE)=' {deploy_dir}/.env || true", timeout=30)
            mysql_root = env.get("MYSQL_ROOT_PASSWORD", "ResumaiRoot!2026")
            mysql_db = env.get("MYSQL_DATABASE", "resumai_agent")
            for line in env_text.splitlines():
                if line.startswith("MYSQL_ROOT_PASSWORD="):
                    mysql_root = line.split("=", 1)[1].strip()
                elif line.startswith("MYSQL_DATABASE="):
                    mysql_db = line.split("=", 1)[1].strip()
            run(
                ssh,
                f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} < {remote_v4}",
                timeout=120,
            )
            print("[migration] applied migration-v4.sql")
            run(ssh, "docker restart ai-resume-backend", timeout=120)
            print("[migration] restarted ai-resume-backend after migration-v4")

        backend_health = ""
        for attempt in range(1, 31):
            backend_health = run(
                ssh,
                "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health",
                timeout=30,
                allow_fail=True,
            )
            print(f"[backend-health] attempt {attempt}: {backend_health[:120]}")
            if "UP" in backend_health:
                break
            time.sleep(6)
        else:
            raise SystemExit("backend container health not UP after restart")

        run(ssh, "curl -fsS http://127.0.0.1/api/health", timeout=30)
        wait_health(host)

        index_html = run(ssh, "curl -fsS http://127.0.0.1/ | head -c 2000", timeout=30)
        bundle_match = re.search(r"/assets/index-([A-Za-z0-9_-]+)\.js", index_html)
        if bundle_match:
            print(f"[frontend] bundle hash: index-{bundle_match.group(1)}.js")
        else:
            print("[warn] could not detect frontend bundle hash from index.html")

        tasks_raw = run(ssh, "curl -fsS 'http://127.0.0.1/api/tasks?page=1&pageSize=5'", timeout=30)
        try:
            tasks_payload = json.loads(tasks_raw)
            items = tasks_payload.get("items") if isinstance(tasks_payload, dict) else tasks_payload
            print(f"[smoke] /api/tasks count={len(items or [])} total={tasks_payload.get('total') if isinstance(tasks_payload, dict) else 'n/a'}")
        except json.JSONDecodeError:
            raise SystemExit("invalid /api/tasks JSON")

        print("\n[ok] sync + rebuild complete — run: python scripts/post_deploy_check.py")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
