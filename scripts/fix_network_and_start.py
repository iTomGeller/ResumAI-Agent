"""Find the correct Docker network and start Langfuse."""
import time
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

def run(ssh, cmd, timeout=600):
    print(f"\n$ {cmd}")
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
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

    print("=== Docker networks ===")
    run(ssh, "docker network ls")

    print("=== Backend container network ===")
    run(ssh, "docker inspect ai-resume-backend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'")

    print("=== Create resumai-net if needed ===")
    run(ssh, "docker network create resumai-net 2>&1 || echo 'network already exists or created'")

    print("=== Connect existing containers to resumai-net ===")
    run(ssh, "docker network connect resumai-net ai-resume-backend 2>&1 || true")

    print("=== Start Langfuse ===")
    run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.langfuse.yml up -d 2>&1", timeout=600)

    print("=== Waiting 60s for startup ===")
    time.sleep(60)

    run(ssh, "docker ps -a --format 'table {{.Names}}\t{{.Status}}' | grep -i langfuse")
    run(ssh, "docker logs langfuse-web --tail 40 2>&1")
    run(ssh, "curl -sS -o /dev/null -w 'HTTP %{http_code}' http://localhost:3001/ 2>&1")

    ssh.close()

if __name__ == "__main__":
    main()
