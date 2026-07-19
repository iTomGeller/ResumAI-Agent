# Volume 与数据持久化

Named volumes（禁止改名/prune/down -v）：
resumai-mysql-data, resumai-redis-data, resumai-neo4j-data,
resumai-workflow-postgres-data, resumai-minio-data, resumai-etcd-data,
resumai-milvus-data, resumai-prometheus-data, resumai-grafana-data,
resumai-uploads-data, resumai-backend-logs

Schema 升级：`DbMigrationRunner` + `db/migrations/V*.sql`（guard 幂等）。
重建容器必须继续挂载原 Volume；部署前 mysqldump 备份。
