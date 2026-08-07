"""Bootstrap ECS .env from .deploy.local.env with safe defaults for missing keys."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULTS = {
    "MYSQL_ROOT_PASSWORD": "ResumaiRoot!2026",
    "MYSQL_DATABASE": "resumai_agent",
    "MYSQL_USER": "resumai",
    "MYSQL_PASSWORD": "ResumaiApp!2026",
    "REDIS_PASSWORD": "ResumaiRedis!2026",
    "NEO4J_AUTH": "neo4j/ResumaiNeo4j!2026",
    "MINIO_ROOT_USER": "minioadmin",
    "MINIO_ROOT_PASSWORD": "ResumaiMinio!2026",
    "WORKFLOW_INTERNAL_TOKEN": "resumai-workflow-internal-2026",
    "WORKFLOW_POSTGRES_PASSWORD": "ResumaiWorkflowPg-2026",
    "PUBLIC_HOST": "8.138.10.189",
    "DEEPSEEK_API_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL": "deepseek-v4-flash",
    "DEEPSEEK_QUALITY_MODEL": "deepseek-v4-pro",
    "LLM_MAX_CONCURRENT": "16",
    "LLM_HTTP_MAX_CONNECTIONS": "16",
    "LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS": "16",
}


def load_env() -> dict[str, str]:
    merged = dict(DEFAULTS)
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        merged[k.strip()] = v.strip()
    # One-off ECS replacements must not require rewriting the ignored local
    # secret file. Explicit process values always win over its previous host.
    for key, value in os.environ.items():
        if value and (key in merged or key in {
                "ALIYUN_HOST", "ALIYUN_USER", "ALIYUN_PASSWORD",
                "DEPLOY_DIR", "DASHSCOPE_API_KEY", "OPENROUTER_API_KEY",
                "EMBEDDING_PROVIDER", "EMBEDDING_ENABLED",
                "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL",
                "EMBEDDING_MODEL", "MILVUS_DIMENSION"}):
            merged[key] = value
    merged["PUBLIC_HOST"] = merged.get("ALIYUN_HOST", merged["PUBLIC_HOST"])
    return merged


def main() -> None:
    env = load_env()
    deploy_dir = env.get("DEPLOY_DIR", "/opt/ai-resume-agent-platform")
    neo4j_user, _, neo4j_pass = env["NEO4J_AUTH"].partition("/")
    content = "\n".join([
        "SPRING_PROFILES_ACTIVE=prod",
        "BACKEND_PORT=8080",
        "FRONTEND_PORT=80",
        "UPLOAD_DIR=/opt/ai-resume-agent-platform/uploads",
        f"DEEPSEEK_API_KEY={env['DEEPSEEK_API_KEY']}",
        f"DEEPSEEK_API_URL={env['DEEPSEEK_API_URL']}",
        f"DEEPSEEK_MODEL={env['DEEPSEEK_MODEL']}",
        f"DEEPSEEK_QUALITY_MODEL={env['DEEPSEEK_QUALITY_MODEL']}",
        f"LLM_MAX_CONCURRENT={env['LLM_MAX_CONCURRENT']}",
        f"LLM_HTTP_MAX_CONNECTIONS={env['LLM_HTTP_MAX_CONNECTIONS']}",
        ("LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS="
         f"{env['LLM_HTTP_MAX_KEEPALIVE_CONNECTIONS']}"),
        f"EMBEDDING_PROVIDER={env.get('EMBEDDING_PROVIDER', 'dashscope')}",
        f"EMBEDDING_ENABLED={env.get('EMBEDDING_ENABLED', 'true')}",
        ("EMBEDDING_API_KEY=" + env.get(
            "EMBEDDING_API_KEY", env.get("DASHSCOPE_API_KEY", ""))),
        ("EMBEDDING_BASE_URL=" + env.get(
            "EMBEDDING_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1")),
        f"EMBEDDING_MODEL={env.get('EMBEDDING_MODEL', 'text-embedding-v3')}",
        "MYSQL_HOST=mysql",
        "MYSQL_PORT=3306",
        f"MYSQL_ROOT_PASSWORD={env['MYSQL_ROOT_PASSWORD']}",
        f"MYSQL_DATABASE={env['MYSQL_DATABASE']}",
        f"MYSQL_USER={env['MYSQL_USER']}",
        f"MYSQL_PASSWORD={env['MYSQL_PASSWORD']}",
        "REDIS_HOST=redis",
        "REDIS_PORT=6379",
        f"REDIS_PASSWORD={env['REDIS_PASSWORD']}",
        "NEO4J_URI=bolt://neo4j:7687",
        f"NEO4J_USERNAME={neo4j_user or 'neo4j'}",
        f"NEO4J_PASSWORD={neo4j_pass or neo4j_user}",
        f"NEO4J_AUTH={env['NEO4J_AUTH']}",
        "MILVUS_HOST=milvus",
        "MILVUS_PORT=19530",
        "MILVUS_COLLECTION=resume_chunk",
        f"MILVUS_DIMENSION={env.get('MILVUS_DIMENSION', '1024')}",
        f"MINIO_ROOT_USER={env['MINIO_ROOT_USER']}",
        f"MINIO_ROOT_PASSWORD={env['MINIO_ROOT_PASSWORD']}",
        f"PUBLIC_HOST={env['PUBLIC_HOST']}",
        f"WORKFLOW_INTERNAL_TOKEN={env['WORKFLOW_INTERNAL_TOKEN']}",
        f"WORKFLOW_POSTGRES_PASSWORD={env['WORKFLOW_POSTGRES_PASSWORD']}",
        "LANGGRAPH_RUNTIME_ENABLED=true",
        "",
    ])

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
    try:
        sftp = ssh.open_sftp()
        remote = f"{deploy_dir}/.env"
        with sftp.open(remote, "w") as f:
            f.write(content)
        sftp.close()
        ssh.exec_command(f"chmod 600 {remote}")
        print(f"[ok] wrote {remote} ({len(content.splitlines())} lines)")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
