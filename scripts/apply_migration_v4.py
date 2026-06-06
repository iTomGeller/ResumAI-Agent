"""Apply migration-v4 on ECS and restart backend (no health pre-check)."""
from pathlib import Path
import sys
import time

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_env():
    env: dict[str, str] = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def run(ssh: paramiko.SSHClient, command: str, timeout: int = 120) -> str:
    print(f"\n$ {command}")
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if err:
        print(err)
    if code != 0:
        raise SystemExit(f"failed ({code}): {command}")
    return out + err


def main() -> None:
    env = load_env()
    root = Path(__file__).resolve().parents[1]
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    local_v4 = root / "backend/src/main/resources/db/migration-v4.sql"
    remote_v4 = f"{deploy_dir}/backend/src/main/resources/db/migration-v4.sql"

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
        sftp.put(str(local_v4), remote_v4)
        sftp.close()

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

        local_repair = root / "backend/src/main/resources/db/repair-queue-status.sql"
        if local_repair.exists():
            remote_repair = f"{deploy_dir}/backend/src/main/resources/db/repair-queue-status.sql"
            sftp = ssh.open_sftp()
            sftp.put(str(local_repair), remote_repair)
            sftp.close()
            run(
                ssh,
                f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} < {remote_repair}",
                timeout=120,
            )
            print("[repair] applied repair-queue-status.sql")

        stats = run(
            ssh,
            f"docker exec resumai-mysql mysql -uroot -p'{mysql_root}' -N -s -e "
            f"\"SELECT status, queue_status, COUNT(*) FROM {mysql_db}.resume_task "
            f"WHERE deleted=0 GROUP BY status, queue_status ORDER BY queue_status, status\"",
            timeout=30,
        ).strip()
        print(f"[check] queue status distribution:\n{stats}")

        run(ssh, "docker restart ai-resume-backend", timeout=120)
        for attempt in range(1, 41):
            _, stdout, stderr = ssh.exec_command(
                "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health",
                timeout=20,
            )
            body = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            print(f"[health] attempt {attempt}: {(body or err)[:120]}")
            if "UP" in body:
                break
            time.sleep(6)
        else:
            _, stdout, _ = ssh.exec_command("docker logs ai-resume-backend --tail 40 2>&1", timeout=30)
            print(stdout.read().decode("utf-8", errors="replace"))
            raise SystemExit("backend still not healthy")

        run(ssh, "curl -fsS http://127.0.0.1/api/task-queue/status", timeout=30)
        print("\n[ok] migration-v4 applied and backend healthy")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
