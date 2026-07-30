"""Check if app containers are still healthy after prune."""
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

def run(ssh, cmd, timeout=300):
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

    print("=== App containers ===")
    run(ssh, "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'ai-resume|resumai'")

    print("=== Backend health ===")
    run(ssh, "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health 2>&1")

    print("=== Frontend health ===")
    run(ssh, "curl -fsS http://127.0.0.1/ -o /dev/null -w '%{http_code}' 2>&1")

    ssh.close()

if __name__ == "__main__":
    main()
