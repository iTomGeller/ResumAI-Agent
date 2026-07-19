# ECS 部署

目录：`/opt/resumai-src`（源码）  
运行 Compose project：`resumai`（复用原 Volume）

```bash
# 在 ECS 上
cd /opt/resumai-src
bash scripts/ecs_safe_deploy.sh
```

约束：
- 禁止本地 npm run build / 本地 docker 部署
- 使用国内 Maven/npm/pip/Docker 镜像
- 不执行 docker system prune -a / volume prune
- 前端构建与镜像构建均在 ECS

回滚：
```bash
# 恢复 DB
docker exec -i resumai-mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" < /root/resumai-backups/<stamp>/mysql-*.sql
# 切换到备份 commit 后重新 build/up（不加 -v）
```
