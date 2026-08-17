"""Sync current workspace to /opt/resumai-src and run ecs_safe_deploy.sh.

Builds happen only on ECS (mvn/npm/docker). Never prints secrets.
"""
from __future__ import annotations

import os
import hashlib
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = "/opt/resumai-src"
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
    # Old local experiments that impersonated MCP/Skills without going through
    # the production registry. They were previously copied by the broad
    # workspace glob and can otherwise survive on the ECS source tree.
    "workflow/app/conversation/copilot_mcp.py",
    "workflow/app/conversation/intent_classifier.py",
    "workflow/app/conversation/react_agent.py",
]

WORKTREE_NEW = [
    "workflow/app/runtime/checkpoint.py",
    "workflow/app/runtime/langgraph_executor.py",
    "workflow/tests/test_langgraph_runtime.py",
    "docs/RAG_THREE_STAGE_EXPERIMENT_PROTOCOL.md",
    "harness/audit_three_stage_rag_chunks.py",
    "harness/rag_three_stage_catalog.py",
    "harness/rag_three_stage_lib.py",
    "harness/run_three_stage_rag_experiments.py",
    "harness/validate_three_stage_benchmark.py",
    "scripts/build_real_jd_rag_corpus.py",
    "scripts/build_three_stage_gold_spans.py",
    "scripts/run_three_stage_rag_ecs.sh",
    "scripts/seed_rag_experiment_jds.py",
    "testdata/rag_three_stage/jd_catalog.json",
    "testdata/rag_three_stage/jd_queries.json",
    "testdata/rag_three_stage/jd_source_receipt.json",
    "testdata/rag_three_stage/knowledge_documents_live.json",
    "testdata/rag_three_stage/knowledge_documents.json",
    "testdata/rag_three_stage/knowledge_queries.json",
    "testdata/rag_three_stage/manifest.json",
    "testdata/rag_three_stage/rag_gold_spans.json",
    "testdata/rag_three_stage/resume_evidence_cases.json",
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    # Explicit one-off target values override a stale ignored deployment file.
    for key in list(env) + [
            "ALIYUN_HOST", "ALIYUN_USER", "ALIYUN_PASSWORD", "SKIP_TESTS"]:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env = load_env()
    deploy_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if len(deploy_sha) != 40 or any(
        c not in "0123456789abcdef" for c in deploy_sha.lower()
    ):
        raise SystemExit("invalid git HEAD; refusing non-reproducible deploy")
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", "HEAD"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    deleted = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=D", "HEAD"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    host = env["ALIYUN_HOST"]
    user = env.get("ALIYUN_USER", "root")
    password = env["ALIYUN_PASSWORD"]
    skip_tests = "1" if env.get("SKIP_TESTS", "0").strip() == "1" else "0"
    openrouter_key = env.get("EMBEDDING_API_KEY") or env.get("OPENROUTER_API_KEY") or ""

    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", deploy_sha],
        cwd=ROOT,
        text=True,
    ).splitlines()
    print(f"[pack] {len(tracked)} committed files from {deploy_sha[:12]}")

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tar_path = Path(tmp.name)
    subprocess.check_call(
        [
            "git", "archive", "--format=tar.gz",
            f"--output={tar_path}", deploy_sha,
        ],
        cwd=ROOT,
    )
    archive_sha256 = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    with tempfile.NamedTemporaryFile(
        suffix=".sha256", mode="w", encoding="utf-8",
        newline="\n", delete=False
    ) as manifest_tmp:
        manifest_path = Path(manifest_tmp.name)
        with tarfile.open(tar_path, "r:gz") as archive:
            for member in sorted(archive.getmembers(), key=lambda item: item.name):
                if not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise SystemExit(f"cannot hash archive member: {member.name}")
                digest = hashlib.sha256(source.read()).hexdigest()
                manifest_tmp.write(f"{digest}  {member.name}\n")
    print(f"[pack] archive={tar_path.stat().st_size // 1024} KB")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, look_for_keys=False,
                allow_agent=False, timeout=30)
    try:
        sftp = ssh.open_sftp()
        remote_tar = "/tmp/resumai-sync.tar.gz"
        remote_manifest = "/tmp/resumai-source.sha256"
        print(f"[upload] -> {remote_tar}")
        sftp.put(str(tar_path), remote_tar)
        sftp.put(str(manifest_path), remote_manifest)
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

        # Build an incoming tree from committed HEAD plus all tracked worktree
        # edits/deletions. Compilation and tests still happen only on ECS.
        incoming = f"{SRC_DIR}.incoming"
        previous = f"{SRC_DIR}.previous"
        run(
            f"echo '{archive_sha256}  {remote_tar}' | sha256sum -c -; "
            f"rm -rf {incoming}; mkdir -p {incoming}; "
            f"tar -xzf {remote_tar} -C {incoming}; "
            f"mv {remote_manifest} {incoming}/.deploy-source.sha256; "
            f"printf '%s\\n' '{deploy_sha}' > {incoming}/.deploy-commit; "
            f"if test -f {SRC_DIR}/.env; then cp -a {SRC_DIR}/.env {incoming}/.env; fi",
            timeout=300,
        )

        sftp = ssh.open_sftp()
        try:
            for relative in sorted(set(changed + WORKTREE_NEW)):
                local_path = ROOT / relative
                if not local_path.is_file():
                    raise SystemExit(f"missing worktree source: {relative}")
                remote_path = f"{incoming}/{relative}"
                run(f"mkdir -p {remote_path.rsplit('/', 1)[0]}", timeout=30)
                sftp.put(str(local_path), remote_path)
        finally:
            sftp.close()

        if deleted:
            delete_targets = " ".join(
                shlex.quote(f"{incoming}/{relative}")
                for relative in deleted
            )
            run(f"rm -f -- {delete_targets}", timeout=60)

        # Swap only source directories. Named Docker volumes are outside this
        # path and remain untouched.
        run(
            f"rm -rf {previous}; "
            f"if test -d {SRC_DIR}; then mv {SRC_DIR} {previous}; fi; "
            f"mv {incoming} {SRC_DIR}; "
            f"rm -f {remote_tar}",
            timeout=180,
        )

        # Remove deleted Java dead-code files + empty agent/tool packages.
        stale_rm = " ".join(f"{SRC_DIR}/{f}" for f in STALE)
        run(
            f"rm -f {stale_rm}; "
            f"rm -rf {SRC_DIR}/backend/src/main/java/com/resumai/agent/ai/agents "
            f"{SRC_DIR}/backend/src/main/java/com/resumai/agent/ai/tools "
            f"{SRC_DIR}/workflow/app/skills; "
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
            # Three-stage held-out winners. Write them explicitly so an old
            # ECS .env cannot override the committed production defaults.
            fh.write("EMBEDDING_DIMENSION=1024\n")
            fh.write("RESUME_EMBEDDING_DIMENSION=1024\n")
            fh.write("JD_EMBEDDING_DIMENSION=768\n")
            fh.write("KB_EMBEDDING_DIMENSION=768\n")
            fh.write("KB_CHUNK_CHARS=320\n")
            fh.write("KB_OVERLAP_CHARS=0\n")
            fh.write("RUN_MAX_GLOBAL_CONCURRENT=24\n")
            fh.write("EMBEDDING_ENABLED=true\n")
            fh.write("CACHE_ENABLED=true\n")
        sftp.close()
        run(
            f"ENV={SRC_DIR}/.env; "
            "while IFS= read -r line; do "
            "  k=${line%%=*}; "
            "  grep -q \"^${k}=\" \"$ENV\" && sed -i \"s|^${k}=.*|${line}|\" \"$ENV\" || echo \"$line\" >> \"$ENV\"; "
            "done < /tmp/embed.env; rm -f /tmp/embed.env; "
            "grep -E '^(EMBEDDING_PROVIDER|EMBEDDING_MODEL|EMBEDDING_BASE_URL|EMBEDDING_DIMENSION|RESUME_EMBEDDING_DIMENSION|JD_EMBEDDING_DIMENSION|KB_EMBEDDING_DIMENSION|KB_CHUNK_CHARS|KB_OVERLAP_CHARS|CACHE_ENABLED)=' \"$ENV\"",
            timeout=30,
        )

        print("\n[lock] generating Python hash lock on ECS")
        run(
            "docker run --rm "
            f"-v {SRC_DIR}/workflow:/work -w /work "
            "docker.m.daocloud.io/library/python:3.12-slim "
            "sh -lc 'pip install -q pip-tools==7.5.1 "
            "-i https://mirrors.aliyun.com/pypi/simple/ && "
            "pip-compile --generate-hashes --strip-extras "
            "--output-file=requirements.lock "
            "--pip-args=\"-i https://mirrors.aliyun.com/pypi/simple/ --timeout 120\" "
            "requirements.txt'",
            timeout=1800,
        )
        # A prior host build can leave ignored dependency/build directories in
        # the source path. They are not part of the committed archive or hash
        # manifest and may contain npm symlinks, which the deploy verifier
        # correctly rejects. Remove only these exact, reproducible build
        # outputs before generating the fresh manifest.
        run(
            f"rm -rf -- {SRC_DIR}/frontend/node_modules "
            f"{SRC_DIR}/frontend/dist {SRC_DIR}/backend/target",
            timeout=120,
        )
        run(
            f"cd {SRC_DIR} && "
            "find . -type f ! -name .env ! -name .deploy-commit "
            "! -name .deploy-source.sha256 -printf '%P\\0' | "
            "LC_ALL=C sort -z | xargs -0 sha256sum > .deploy-source.sha256",
            timeout=120,
        )
        sftp = ssh.open_sftp()
        try:
            sftp.get(
                f"{SRC_DIR}/workflow/requirements.lock",
                str(ROOT / "workflow/requirements.lock"),
            )
        finally:
            sftp.close()

        print("\n[deploy] starting ecs_safe_deploy.sh (long)")
        run(
            f"cd {SRC_DIR} && DEPLOY_SHA={deploy_sha} "
            f"SKIP_TESTS={skip_tests} bash scripts/ecs_safe_deploy.sh",
            timeout=7200,
        )
        print("\n[ok] ECS deploy finished")
    finally:
        ssh.close()
        try:
            tar_path.unlink(missing_ok=True)
        except TypeError:
            if tar_path.exists():
                tar_path.unlink()
        try:
            manifest_path.unlink(missing_ok=True)
        except TypeError:
            if manifest_path.exists():
                manifest_path.unlink()


if __name__ == "__main__":
    # pathlib Path.unlink(missing_ok) needs 3.8+; provide fallback above
    main()
