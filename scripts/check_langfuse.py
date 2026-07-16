"""Check Langfuse container status on ECS and optionally start it."""
import sys
from pathlib import Path
import paramiko

def load_env():
    env = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env

def run(ssh, cmd):
    print(f"\n$ {cmd}")
    _, out, err = ssh.exec_command(cmd, timeout=300)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    if o: print(o)
    if e: print(e)
    return o + e

def main():
    env = load_env()
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"),
                password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)

    run(ssh, "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | head -30")
    run(ssh, f"ls -la {deploy_dir}/docker-compose.langfuse.yml 2>&1")
    run(ssh, f"cat {deploy_dir}/docker-compose.langfuse.yml | head -5")

    if "--start" in sys.argv:
        print("\n=== Starting Langfuse stack ===")
        run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.langfuse.yml up -d 2>&1")
        import time
        time.sleep(10)
        run(ssh, "docker ps -a --format 'table {{.Names}}\t{{.Status}}' | grep -i langfuse")
        run(ssh, "docker logs langfuse-web --tail 20 2>&1")
    else:
        run(ssh, "docker logs langfuse-web --tail 20 2>&1")

    run(ssh, "curl -sS -o /dev/null -w 'HTTP %{http_code}' http://localhost:3001/ 2>&1")
    run(ssh, "docker exec ai-resume-workflow python - <<'PY'\n"
             "import urllib.request\n"
             "url='http://langfuse-web:3000'\n"
             "try:\n"
             "    r=urllib.request.urlopen(url, timeout=5)\n"
             "    print('workflow_to_langfuse HTTP', r.status)\n"
             "except Exception as e:\n"
             "    print('workflow_to_langfuse FAILED', e)\n"
             "PY")
    run(ssh, "echo 'Langfuse public URL: http://8.138.10.189:3001'")

    ssh.close()

if __name__ == "__main__":
    main()
