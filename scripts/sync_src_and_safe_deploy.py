"""Sync current workspace to /opt/resumai-src and run ecs_safe_deploy.sh.

Builds happen only on ECS (mvn/npm/docker). Never prints secrets.
"""
from __future__ import annotations

import os
import sys
import tarfile
import tempfile
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = "/opt/resumai-src"
INCLUDE_PREFIXES = (
    "backend/src/",
    "backend/pom.xml",
    "backend/Dockerfile",
    "backend/skills/",
    "frontend/src/",
    "frontend/nginx.conf",
    "frontend/Dockerfile",
    "frontend/Dockerfile.ecs",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/tsconfig.node.json",
    "frontend/vite.config.ts",
    "frontend/index.html",
    "workflow/",
    "harness/",
    "config/",
    "scripts/ecs_safe_deploy.sh",
    "scripts/seed_knowledge_base.py",
    "scripts/ecs_acceptance.sh",
    "testdata/benchmark/",
    "docker-compose.prod.yml",
    "docker-compose.ecs.yml",
    "docs/WHY_UNIFIED_RUNTIME.md",
)
EXCLUDE_PARTS = (
    "/__pycache__/",
    "/.pytest_cache/",
    "/node_modules/",
    "/target/",
    "/dist/",
    ".pyc",
)

# Files deleted locally that must be removed on ECS too.
STALE = [
    "backend/src/main/java/com/resumai/agent/ai/ResumeEvaluationOrchestrator.java",
    "backend/src/main/java/com/resumai/agent/ai/McpToolRegistry.java",
    "backend/src/main/java/com/resumai/agent/ai/AgentTraceCapture.java",
    "backend/src/main/java/com/resumai/agent/ai/TracingChatModelListener.java",
    "backend/src/main/java/com/resumai/agent/ai/AgentExecutionContext.java",
    "backend/src/main/java/com/resumai/agent/api/McpController.java",
    "backend/src/main/java/com/resumai/agent/api/GraphController.java",
    "backend/src/main/java/com/resumai/agent/api/dto/GraphResponse.java",
    "backend/src/main/java/com/resumai/agent/config/Neo4jConfig.java",
    "backend/src/main/java/com/resumai/agent/config/Neo4jProperties.java",
    "backend/src/main/java/com/resumai/agent/service/ResumeGraphService.java",
    "backend/src/main/java/com/resumai/agent/domain/enums/RagStrategyType.java",
    "backend/src/test/java/com/resumai/agent/ai/McpToolRegistryTest.java",
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def wanted(rel: str) -> bool:
    if any(p in f"/{rel}/" or rel.endswith(p) for p in EXCLUDE_PARTS):
        return False
    return any(rel == p or rel.startswith(p) for p in INCLUDE_PREFIXES)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env = load_env()
    host = env["ALIYUN_HOST"]
    user = env.get("ALIYUN_USER", "root")
    password = env["ALIYUN_PASSWORD"]
    openrouter_key = env.get("EMBEDDING_API_KEY") or env.get("OPENROUTER_API_KEY") or ""

    files = [p for p in ROOT.rglob("*") if p.is_file() and wanted(p.relative_to(ROOT).as_posix())]
    # Also wipe deleted agent/tools dirs by including empty markers via cleanup below.
    print(f"[pack] {len(files)} files")

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tar_path = Path(tmp.name)
    with tarfile.open(tar_path, "w:gz") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(ROOT).as_posix())
    print(f"[pack] archive={tar_path.stat().st_size // 1024} KB")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, look_for_keys=False,
                allow_agent=False, timeout=30)
    try:
        sftp = ssh.open_sftp()
        remote_tar = "/tmp/resumai-sync.tar.gz"
        print(f"[upload] -> {remote_tar}")
        sftp.put(str(tar_path), remote_tar)
        sftp.close()

        def run(cmd: str, timeout: int = 7200) -> str:
            print(f"\n$ {cmd[:200]}{'...' if len(cmd) > 200 else ''}")
            _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            if out:
                print(out[-8000:] if len(out) > 8000 else out)
            if err:
                print(err[-4000:] if len(err) > 4000 else err)
            if code != 0:
                raise SystemExit(f"remote failed ({code})")
            return out + err

        run(f"mkdir -p {SRC_DIR} && tar -xzf {remote_tar} -C {SRC_DIR} && rm -f {remote_tar}")

        # Remove deleted Java dead-code files + empty agent/tool packages.
        stale_rm = " ".join(f"{SRC_DIR}/{f}" for f in STALE)
        run(
            f"rm -f {stale_rm}; "
            f"rm -rf {SRC_DIR}/backend/src/main/java/com/resumai/agent/ai/agents "
            f"{SRC_DIR}/backend/src/main/java/com/resumai/agent/ai/tools; "
            f"chmod +x {SRC_DIR}/scripts/ecs_safe_deploy.sh",
            timeout=60,
        )

        # Prefer Bailian (DashScope) when configured — reachable from this ECS.
        # OpenRouter remains optional via host :8443 tunnel when explicitly selected.
        provider = (env.get("EMBEDDING_PROVIDER") or "bailian").strip().lower()
        bailian_key = (
            env.get("DASHSCOPE_API_KEY")
            or env.get("BAILIAN_API_KEY")
            or (env.get("EMBEDDING_API_KEY") if provider == "bailian" else "")
            or ""
        ).strip()
        if provider == "bailian":
            model = (env.get("EMBEDDING_MODEL") or "text-embedding-v3").strip()
            base = (env.get("EMBEDDING_BASE_URL")
                    or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
            embed_key = bailian_key
        else:
            model = (env.get("EMBEDDING_MODEL") or "qwen/qwen3-embedding-4b").strip()
            base = (env.get("EMBEDDING_BASE_URL") or "https://openrouter.ai:8443/api/v1").strip()
            embed_key = openrouter_key if openrouter_key.startswith("sk-or-") else bailian_key
        run(
            f"test -f {SRC_DIR}/.env || cp -a /opt/ai-resume-agent-platform/.env {SRC_DIR}/.env; "
            f"touch {SRC_DIR}/.env",
            timeout=30,
        )
        sftp = ssh.open_sftp()
        with sftp.file("/tmp/embed.env", "w") as fh:
            if openrouter_key.startswith("sk-or-"):
                fh.write(f"OPENROUTER_API_KEY={openrouter_key}\n")
            if bailian_key:
                fh.write(f"DASHSCOPE_API_KEY={bailian_key}\n")
            if embed_key:
                fh.write(f"EMBEDDING_API_KEY={embed_key}\n")
            fh.write(f"EMBEDDING_PROVIDER={provider}\n")
            fh.write(f"EMBEDDING_BASE_URL={base}\n")
            fh.write(f"EMBEDDING_MODEL={model}\n")
            fh.write("EMBEDDING_ENABLED=true\n")
            fh.write("CACHE_ENABLED=true\n")
        sftp.close()
        run(
            f"ENV={SRC_DIR}/.env; "
            "while IFS= read -r line; do "
            "  k=${line%%=*}; "
            "  grep -q \"^${k}=\" \"$ENV\" && sed -i \"s|^${k}=.*|${line}|\" \"$ENV\" || echo \"$line\" >> \"$ENV\"; "
            "done < /tmp/embed.env; rm -f /tmp/embed.env; "
            "grep -E '^(EMBEDDING_PROVIDER|EMBEDDING_MODEL|EMBEDDING_BASE_URL|CACHE_ENABLED)=' \"$ENV\"",
            timeout=30,
        )

        print("\n[deploy] starting ecs_safe_deploy.sh (long)")
        run(f"cd {SRC_DIR} && bash scripts/ecs_safe_deploy.sh", timeout=7200)
        print("\n[ok] ECS deploy finished")
    finally:
        ssh.close()
        try:
            tar_path.unlink(missing_ok=True)
        except TypeError:
            if tar_path.exists():
                tar_path.unlink()


if __name__ == "__main__":
    # pathlib Path.unlink(missing_ok) needs 3.8+; provide fallback above
    main()
