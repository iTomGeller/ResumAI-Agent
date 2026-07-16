"""Debug Langfuse trace ingestion from workflow container on ECS."""
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
    _, out, err = ssh.exec_command(cmd, timeout=180)
    o = out.read().decode("utf-8", errors="replace")
    e = err.read().decode("utf-8", errors="replace")
    if o:
        print(o)
    if e:
        print(e)
    return o + e


def main():
    env = load_env()
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

    run(ssh, "docker exec ai-resume-workflow printenv | grep LANGFUSE || true")
    run(ssh, "docker logs ai-resume-workflow 2>&1 | grep -i langfuse | tail -40 || true")

    sdk_test = r"""docker exec ai-resume-workflow python - <<'PY'
import os
print('env keys', bool(os.getenv('LANGFUSE_PUBLIC_KEY')), bool(os.getenv('LANGFUSE_SECRET_KEY')))
print('host', os.getenv('LANGFUSE_HOST'))
try:
    import langfuse
    print('langfuse version', getattr(langfuse, '__version__', 'unknown'))
    from langfuse import Langfuse
    lf = Langfuse(
        public_key=os.getenv('LANGFUSE_PUBLIC_KEY', 'lf_pk_resumai'),
        secret_key=os.getenv('LANGFUSE_SECRET_KEY', 'lf_sk_resumai'),
        host=os.getenv('LANGFUSE_HOST', 'http://langfuse-web:3000'),
    )
    trace_id = 'test-trace-manual-001'
    t = lf.trace(id=trace_id, input='hello from workflow container')
    lf.flush()
    print('SDK trace() ok', t.id)
except Exception as exc:
    print('SDK FAILED', type(exc).__name__, exc)
PY"""
    run(ssh, sdk_test)

    run(ssh, "docker logs langfuse-web 2>&1 | grep -i 'test-trace-manual' | tail -10 || true")
    run(ssh, "docker logs langfuse-worker 2>&1 | tail -40")

    ssh.close()


if __name__ == "__main__":
    main()
