# ECS 部署

目录：`/opt/resumai-src`（源码 + 构建 + 运行）  
Compose project：`resumai`（复用原有 named volumes）

```bash
# 在 ECS 上
cd /opt/resumai-src
bash scripts/ecs_safe_deploy.sh
```

脚本内置步骤：
1. preflight（磁盘/内存/容器/卷/commit）
2. 备份 .env + gzip 压缩的 mysqldump（`/root/resumai-backups/<stamp>/`）；部署验收成功后默认保留最新 7 份（可用 `BACKUP_KEEP` 调整）
3. `SANDBOX_WORKER_TAG=<git sha>` 写入 .env（Worker 镜像禁止 latest）
4. compose config 校验（含旧 runtime 防回流 grep）
5. JDK21 `mvn clean package`（阿里云 Maven 镜像）
6. `npm ci`（npmmirror）→ `npm run build`（仅在 ECS）
7. Sandbox Worker（SHA tag）/ Manager / Workflow / Backend / Frontend 镜像构建
   （workflow 镜像构建期强制 pytest + 契约门禁）
8. `up -d`（原卷复用；旧 checkpoint postgres 若在运行则停止、卷保留）
9. 健康检查 + resume_task 行数前后比对（下降即失败退出）

禁止：`docker compose down -v`、`docker volume prune`、
`docker system prune --volumes`、latest Sandbox 镜像、本地构建部署。

Schema：仅 `db/migrations/V*.sql`（guard 幂等）在 backend 启动时按需应用；
无结构变化时跳过。

验收：`bash scripts/ecs_acceptance.sh`；
真实质量基准：`python3 harness/run_agent_e2e_benchmark.py --base http://127.0.0.1`。

回滚：
```bash
docker exec -i resumai-mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" \
  < <(gzip -dc /root/resumai-backups/<stamp>/mysql-*.sql.gz)
cd /opt/resumai-src && git checkout <上一个稳定 commit> && bash scripts/ecs_safe_deploy.sh
```
