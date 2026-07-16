"""Sync workflow requirements and rebuild workflow container on ECS."""
from pathlib import Path
import paramiko
import subprocess
import sys
import time


def load_env():
    env = {}
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def run(ssh, cmd, timeout=3600):
    print(f"\n$ {cmd}")
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    code = out.channel.recv_exit_status()
    if o:
        print(o[-4000:] if len(o) > 4000 else o)
    if e:
        print(e[-2000:] if len(e) > 2000 else e)
    if code != 0:
        raise SystemExit(f"failed ({code}): {cmd}")
    return o + e


def main():
    env = load_env()
    root = Path(__file__).resolve().parents[1]
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        env["ALIYUN_HOST"],
        username=env.get("ALIYUN_USER", "root"),
        password=env["ALIYUN_PASSWORD"],
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    sftp = ssh.open_sftp()
    local = root / "workflow" / "requirements.txt"
    remote = f"{deploy_dir}/workflow/requirements.txt"
    sftp.put(str(local), remote)
    sftp.close()
    print(f"uploaded {local.name}")

    run(
        ssh,
        f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml build --no-cache ai-resume-workflow",
        timeout=3600,
    )
    run(
        ssh,
        f"cd {deploy_dir} && docker compose -f docker-compose.prod.yml up -d ai-resume-workflow",
        timeout=300,
    )
    time.sleep(8)
    run(ssh, "docker exec ai-resume-workflow python -c \"import langfuse; print('langfuse', langfuse.__version__); from langfuse import Langfuse; lf=Langfuse(public_key='lf_pk_resumai', secret_key='lf_sk_resumai', host='http://langfuse-web:3000'); print('has trace', hasattr(lf, 'trace'))\"")
    ssh.close()

    verify = root / "scripts" / "verify_langgraph_workflow.py"
    host = env["ALIYUN_HOST"]
    print("\n=== running strict verify ===")
    subprocess.run(
        [sys.executable, str(verify), f"http://{host}", "--strict-trace"],
        cwd=root,
        check=True,
    )


if __name__ == "__main__":
    main()
