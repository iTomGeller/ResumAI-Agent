"""Check backend startup status."""
import sys
from pathlib import Path
import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

env = {}
root = Path(__file__).resolve().parents[1]
for line in root.joinpath(".deploy.local.env").read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(env["ALIYUN_HOST"], username=env.get("ALIYUN_USER", "root"),
            password=env["ALIYUN_PASSWORD"], look_for_keys=False, allow_agent=False, timeout=30)

_, stdout, _ = ssh.exec_command(
    'docker logs ai-resume-backend 2>&1 | grep -iE "Started|FAILED|table.*not exist|schema" | tail -10',
    timeout=15
)
print("=== Startup keywords ===")
print(stdout.read().decode("utf-8", "replace"))

_, stdout, _ = ssh.exec_command(
    "docker exec ai-resume-backend curl -fsS http://127.0.0.1:8080/api/health 2>&1",
    timeout=15
)
print("=== Health check ===")
print(stdout.read().decode("utf-8", "replace"))

ssh.close()
