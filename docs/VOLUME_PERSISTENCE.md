# Volume 与数据持久化

当前挂载的 Named volumes（禁止改名 / prune / down -v）：
resumai-mysql-data, resumai-redis-data, resumai-neo4j-data,
resumai-neo4j-logs, resumai-neo4j-plugins, resumai-minio-data,
resumai-etcd-data, resumai-milvus-data, resumai-prometheus-data,
resumai-grafana-data, resumai-uploads-data, resumai-backend-logs

历史卷 `resumai-workflow-postgres-data`（旧图执行 checkpoint 存储）已随旧
Runtime 下线：不再被任何服务挂载，但**保留在磁盘上不删除**（部署脚本永不
prune 任何卷）。pause/resume 快照现存 MySQL `agent_run.execution_snapshot`。

Schema 升级：`DbMigrationRunner` + `db/migrations/V*.sql`（guard 幂等，
V5 会话 revision、V6 Agent Runtime、V7 pause/resume 与 resume_task 桥接）。
只有真实表结构变化才执行；无变化时启动扫描后整体跳过。

部署安全序：备份(.env + mysqldump) → 构建 → up -d（复用原卷）→
校验挂载与关键表行数前后一致（`scripts/ecs_safe_deploy.sh` 内置，
行数下降立即失败退出）。
