"""Reset stuck task and check worker status."""
import sys
import time
from pathlib import Path
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

def run(ssh, cmd, timeout=120):
    print(f"\n$ {cmd}")
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    if o: print(o)
    if e: print(e)
    return o + e

def main():
    env = load_env()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"),
                password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)

    print("=== Backend logs: task worker activity (last 30) ===")
    run(ssh, "docker logs ai-resume-backend --tail 30 2>&1 | grep -i 'worker\\|task\\|queue\\|evaluat' | tail -15")

    print("=== Reset stuck task 1034 to QUEUED ===")
    mysql_root = "ResumaiRoot!2026"
    run(ssh, f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' resumai_agent -e \"UPDATE resume_task SET status='PENDING', queue_status='QUEUED', started_at=NULL, worker_id=NULL WHERE id=1034 AND status='RUNNING';\"")

    print("=== Waiting 10s for worker to pick up task ===")
    time.sleep(10)

    print("=== Backend logs after reset ===")
    run(ssh, "docker logs ai-resume-backend --tail 20 2>&1 | grep -i 'worker\\|task\\|queue\\|evaluat\\|otel\\|span' | tail -15")

    print("=== Check task 1034 status ===")
    run(ssh, f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' resumai_agent -e \"SELECT id, status, queue_status, started_at, finished_at FROM resume_task WHERE id=1034;\"")

    ssh.close()

if __name__ == "__main__":
    main()
