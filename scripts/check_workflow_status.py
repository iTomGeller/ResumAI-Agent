"""Check ECS workflow rebuild status."""
from pathlib import Path
import paramiko


def load_env():
    env = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def run(ssh, cmd):
    print(f"\n$ {cmd}")
    _, out, err = ssh.exec_command(cmd, timeout=120)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    if o:
        print(o[-3000:])
    if e:
        print(e[-1000:])
    return o


def main():
    env = load_env()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env["ALIYUN_HOST"], username="root", password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False)
    run(ssh, "ps aux | grep -E 'docker|compose' | grep -v grep | head -10")
    run(ssh, "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep workflow")
    run(ssh, "docker exec ai-resume-workflow python -c \"import langfuse; print(langfuse.__version__); from langfuse import Langfuse; lf=Langfuse(); print('trace', hasattr(lf,'trace'))\" 2>&1 || true")
    ssh.close()


if __name__ == "__main__":
    main()
