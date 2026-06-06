"""Check task worker status and full backend logs."""
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

    print("=== Full backend startup logs (errors/warnings) ===")
    run(ssh, "docker logs ai-resume-backend 2>&1 | grep -i 'error\\|warn\\|exception\\|worker\\|task-queue\\|scheduled\\|poll' | tail -30")

    print("=== Full backend logs (last 50) ===")
    run(ssh, "docker logs ai-resume-backend --tail 50 2>&1")

    print("=== TaskWorkerService search ===")
    run(ssh, "docker logs ai-resume-backend 2>&1 | grep -i 'TaskWorker\\|taskWorker\\|worker' | tail -10")

    print("=== Trigger queue poll manually ===")
    run(ssh, "curl -sS -X POST 'http://127.0.0.1:8080/api/queue/poll' 2>&1 || curl -sS 'http://127.0.0.1:8080/api/queue/status' 2>&1")

    ssh.close()

if __name__ == "__main__":
    main()
