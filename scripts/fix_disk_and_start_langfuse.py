"""Free disk space on ECS and start Langfuse stack."""
import sys
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

def run(ssh, cmd, timeout=600, allow_fail=False):
    print(f"\n$ {cmd}")
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    code = out.channel.recv_exit_status()
    if o: print(o)
    if e: print(e)
    if code != 0 and not allow_fail:
        print(f"[warn] exit code {code}")
    return o + e

def main():
    env = load_env()
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"),
                password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)

    print("=== Disk usage BEFORE cleanup ===")
    run(ssh, "df -h /")

    print("\n=== Docker disk usage ===")
    run(ssh, "docker system df")

    print("\n=== Cleaning up Docker resources ===")
    run(ssh, "docker system prune -af --volumes --filter 'label!=keep'", allow_fail=True)
    run(ssh, "docker builder prune -af", allow_fail=True)

    print("\n=== Disk usage AFTER cleanup ===")
    run(ssh, "df -h /")

    print("\n=== Starting Langfuse stack ===")
    result = run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.langfuse.yml pull 2>&1", timeout=1200)
    if "error" in result.lower() and "no space" in result.lower():
        print("[FATAL] Still no space after cleanup!")
        ssh.close()
        return

    run(ssh, f"cd {deploy_dir} && docker compose -f docker-compose.langfuse.yml up -d 2>&1", timeout=600)

    print("\n=== Waiting for containers to start (60s) ===")
    time.sleep(60)

    run(ssh, "docker ps -a --format 'table {{.Names}}\t{{.Status}}' | grep -i langfuse")
    run(ssh, "docker logs langfuse-web --tail 30 2>&1")
    run(ssh, "curl -sS -o /dev/null -w 'HTTP %{http_code}' http://localhost:3001/ 2>&1")

    ssh.close()

if __name__ == "__main__":
    main()
