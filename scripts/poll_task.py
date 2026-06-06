"""Poll task 1034 status and check for Langfuse traces."""
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

def run(ssh, cmd, timeout=60):
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

    mysql_root = "ResumaiRoot!2026"

    for attempt in range(12):
        print(f"\n--- Poll attempt {attempt + 1} ---")
        result = run(ssh, f"docker exec -i resumai-mysql mysql -uroot -p'{mysql_root}' resumai_agent -e \"SELECT id, status, queue_status FROM resume_task WHERE id=1034;\" 2>/dev/null")
        
        if "SUCCESS" in result or "FAILED" in result:
            print("Task completed!")
            break
        
        run(ssh, "docker logs ai-resume-backend --tail 5 2>&1")
        time.sleep(10)

    print("\n=== Langfuse traces ===")
    run(ssh, "curl -sS -u 'lf_pk_resumai:lf_sk_resumai' 'http://localhost:3001/api/public/traces?limit=5' 2>&1 | head -c 3000")

    ssh.close()

if __name__ == "__main__":
    main()
