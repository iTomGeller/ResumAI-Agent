"""Verify Langfuse is accessible and fix network connectivity."""
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

    print("=== Check Langfuse web UI ===")
    result = run(ssh, "curl -sS -o /dev/null -w '%{http_code}' http://localhost:3001/ 2>&1")
    print(f"Langfuse HTTP: {result.strip()}")

    print("=== Langfuse-web logs (last 15) ===")
    run(ssh, "docker logs langfuse-web --tail 15 2>&1 | tr -d '\\xE2\\x9C\\x93\\xE2\\x9C\\x97'")

    print("=== Check if backend can reach langfuse-web ===")
    run(ssh, "docker exec ai-resume-backend curl -sS -o /dev/null -w '%{http_code}' http://langfuse-web:3000/ 2>&1")

    print("=== Backend networks ===")
    run(ssh, "docker inspect ai-resume-backend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'")

    print("=== Langfuse-web networks ===")
    run(ssh, "docker inspect langfuse-web --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'")

    ssh.close()

if __name__ == "__main__":
    main()
