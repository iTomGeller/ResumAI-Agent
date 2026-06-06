"""Verify Langfuse has traces and check backend OTel status."""
import sys
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

    print("=== Backend OTel logs ===")
    run(ssh, "docker logs ai-resume-backend 2>&1 | grep -i 'langfuse\\|otel' | tail -5")

    print("=== Langfuse containers ===")
    run(ssh, "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep langfuse")

    print("=== Langfuse Web UI accessible? ===")
    run(ssh, "curl -sS -o /dev/null -w 'HTTP %{http_code}' http://localhost:3001/ 2>&1")

    print("=== Check Langfuse API for traces ===")
    run(ssh, "curl -sS -u 'lf_pk_resumai:lf_sk_resumai' 'http://localhost:3001/api/public/traces?limit=5' 2>&1 | head -c 2000")

    print("=== Check task 1034 status ===")
    run(ssh, "curl -fsS 'http://127.0.0.1/api/tasks?page=1&pageSize=1' 2>&1 | python3 -c 'import sys,json; d=json.load(sys.stdin); t=d[\"items\"][0]; print(f\"id={t[\"id\"]} status={t[\"status\"]} traceId={t[\"traceId\"]}\")'", timeout=30)

    ssh.close()

if __name__ == "__main__":
    main()
