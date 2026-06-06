"""Repair ECS queue status data and drop mismatched Milvus JD collection."""
from pathlib import Path
import socket
import sys
import time

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_env():
    env = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def run(ssh, command, timeout=120, allow_fail=False):
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
        raise SystemExit(f"failed ({code}): {command}")
    return out + err


def main():
    env = load_env()
    root = Path(__file__).resolve().parents[1]
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    local_repair = root / "backend/src/main/resources/db/repair-queue-status.sql"
    local_stuck = root / "backend/src/main/resources/db/repair-stuck-running.sql"
    remote_repair = f"{deploy_dir}/backend/src/main/resources/db/repair-queue-status.sql"
    remote_stuck = f"{deploy_dir}/backend/src/main/resources/db/repair-stuck-running.sql"

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
        env.setdefault("MILVUS_DIMENSION", "0")
        sftp = ssh.open_sftp()
        sftp.put(str(local_repair), remote_repair)
        sftp.put(str(local_stuck), remote_stuck)
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
            f"grep -q '^MILVUS_DIMENSION=' {deploy_dir}/.env && "
            f"sed -i 's/^MILVUS_DIMENSION=.*/MILVUS_DIMENSION=0/' {deploy_dir}/.env || "
            f"echo 'MILVUS_DIMENSION=0' >> {deploy_dir}/.env",
            timeout=30,
        )

        run(
            ssh,
            f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} < {remote_repair}",
            timeout=120,
        )
        run(
            ssh,
            f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' {mysql_db} < {remote_stuck}",
            timeout=120,
        )

        stats = run(
            ssh,
            f"docker exec resumai-mysql mysql -uroot -p'{mysql_root}' -N -s -e "
            f"\"SELECT status, queue_status, COUNT(*) FROM {mysql_db}.resume_task "
            f"WHERE deleted=0 GROUP BY status, queue_status ORDER BY queue_status, status\"",
            timeout=30,
        )
        print(f"[check] queue distribution:\n{stats}")

        drop_cmd = (
            "timeout 120 docker run --rm --network resumai_resumai-net python:3.11-slim bash -lc "
            "\"pip install -q pymilvus && python - <<'PY'\n"
            "from pymilvus import connections, utility\n"
            "connections.connect(host='milvus', port='19530')\n"
            "for name in ('jd_library_local_384', 'resume_chunk_local_384'):\n"
            "    if utility.has_collection(name):\n"
            "        utility.drop_collection(name)\n"
            "        print('dropped', name)\n"
            "    else:\n"
            "        print('skip', name)\n"
            "PY\" || true"
        )
        try:
            run(ssh, drop_cmd, timeout=180, allow_fail=True)
        except (TimeoutError, socket.timeout):
            print("[warn] milvus collection drop timed out; continuing")

        run(ssh, "docker restart ai-resume-backend", timeout=120)
        for attempt in range(1, 31):
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
            raise SystemExit("backend not healthy after repair")

        queue = run(ssh, "curl -fsS http://127.0.0.1/api/task-queue/status", timeout=30)
        print(f"[ok] task-queue/status: {queue.strip()}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
