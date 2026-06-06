"""Re-enqueue task 1034 into Redis stream for processing."""
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
    redis_pw = env.get("REDIS_PASSWORD", "")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"),
                password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)

    print("=== Get Redis password from .env ===")
    redis_pw_result = run(ssh, "grep '^REDIS_PASSWORD=' /opt/ai-resume-agent-platform/.env || true")
    if "REDIS_PASSWORD=" in redis_pw_result:
        redis_pw = redis_pw_result.strip().split("=", 1)[1].strip()

    auth_arg = f"-a '{redis_pw}'" if redis_pw else ""

    print("=== Enqueue task to Redis stream ===")
    run(ssh, f"docker exec resumai-redis redis-cli {auth_arg} XADD resumai:task_queue '*' traceId trace-bf604aee-4d9f-499a-97c1-1d46bf4bc954 taskId 1034 tenantId default uploadedBy demo-hr priority 0")

    print("=== Verify stream contents ===")
    run(ssh, f"docker exec resumai-redis redis-cli {auth_arg} XLEN resumai:task_queue")

    print("=== Waiting 30s for worker to pick up ===")
    time.sleep(30)

    print("=== Check task status ===")
    mysql_root = "ResumaiRoot!2026"
    run(ssh, f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' resumai_agent -e \"SELECT id, status, queue_status, worker_id FROM resume_task WHERE id=1034;\" 2>/dev/null")

    print("=== Backend logs (last 15) ===")
    run(ssh, "docker logs ai-resume-backend --tail 15 2>&1")

    ssh.close()

if __name__ == "__main__":
    main()
