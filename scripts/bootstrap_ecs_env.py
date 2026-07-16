"""Bootstrap ECS .env from .deploy.local.env with safe defaults for missing keys."""
from __future__ import annotations

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
    "GRAFANA_PASSWORD": "ResumaiGrafana!2026",
    "WORKFLOW_INTERNAL_TOKEN": "resumai-workflow-internal-2026",
    "WORKFLOW_POSTGRES_PASSWORD": "ResumaiWorkflowPg!2026",
    "PUBLIC_HOST": "8.138.10.189",
    "LANGFUSE_PUBLIC_URL": "http://8.138.10.189:3001",
    "LANGFUSE_HOST": "http://langfuse-web:3000",
    "LANGFUSE_PUBLIC_KEY": "lf_pk_resumai",
    "LANGFUSE_SECRET_KEY": "lf_sk_resumai",
    "DEEPSEEK_API_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL": "deepseek-chat",
}


def load_env() -> dict[str, str]:
    merged = dict(DEFAULTS)
    path = Path(__file__).resolve().parents[1] / ".deploy.local.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        merged[k.strip()] = v.strip()
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
        "EMBEDDING_PROVIDER=local",
        "EMBEDDING_ENABLED=true",
        f"EMBEDDING_API_KEY={env['DEEPSEEK_API_KEY']}",
        f"EMBEDDING_BASE_URL={env['DEEPSEEK_API_URL']}",
        "EMBEDDING_MODEL=all-minilm-l6-v2",
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
        "MILVUS_DIMENSION=384",
        f"MINIO_ROOT_USER={env['MINIO_ROOT_USER']}",
        f"MINIO_ROOT_PASSWORD={env['MINIO_ROOT_PASSWORD']}",
        f"GRAFANA_PASSWORD={env['GRAFANA_PASSWORD']}",
        f"PUBLIC_HOST={env['PUBLIC_HOST']}",
        f"LANGFUSE_PUBLIC_URL={env['LANGFUSE_PUBLIC_URL']}",
        f"LANGFUSE_HOST={env['LANGFUSE_HOST']}",
        f"LANGFUSE_PUBLIC_KEY={env['LANGFUSE_PUBLIC_KEY']}",
        f"LANGFUSE_SECRET_KEY={env['LANGFUSE_SECRET_KEY']}",
        f"WORKFLOW_INTERNAL_TOKEN={env['WORKFLOW_INTERNAL_TOKEN']}",
        f"WORKFLOW_POSTGRES_PASSWORD={env['WORKFLOW_POSTGRES_PASSWORD']}",
        "OBJECT_STORAGE_ENABLED=false",
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
