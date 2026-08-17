#!/bin/sh
set -u

OUT_FILE="${1:-/tmp/resumai-benchmark-monitor.csv}"
STOP_FILE="${2:-/tmp/resumai-benchmark-monitor.stop}"
INTERVAL_SECONDS="${3:-5}"

mkdir -p "$(dirname "$OUT_FILE")"
rm -f "$STOP_FILE"
printf '%s\n' 'timestamp|docker_stats|container_state|backend_proc|workflow_proc|workflow_health|task_queue|run_queue|run_permits|mysql|redis|postgres|postgres_checkpoints|disk' > "$OUT_FILE"

one_line() {
  tr '\r\n|' ',,;' | sed 's/,,*/,/g; s/,$//'
}

while [ ! -f "$STOP_FILE" ]; do
  cycle_started="$(date +%s)"
  timestamp="$(date --iso-8601=seconds)"
  docker_stats="$(
    docker stats --no-stream \
      --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}' \
      ai-resume-backend ai-resume-workflow resumai-mysql resumai-redis \
      resumai-langgraph-postgres resumai-milvus resumai-etcd resumai-minio \
      2>/dev/null | one_line
  )"
  container_state="$(
    docker inspect \
      --format '{{.Name}},restarts={{.RestartCount}},oom={{.State.OOMKilled}},status={{.State.Status}}' \
      ai-resume-backend ai-resume-workflow resumai-mysql resumai-redis \
      resumai-langgraph-postgres resumai-milvus resumai-etcd resumai-minio \
      2>/dev/null | one_line
  )"
  backend_proc="$(
    docker exec ai-resume-backend sh -lc '
      grep -E "^(VmRSS|VmHWM|Threads):" /proc/1/status
      printf "open_fds:%s\n" "$(ls /proc/1/fd 2>/dev/null | wc -l)"
      grep -E "^(nr_throttled|throttled_usec) " /sys/fs/cgroup/cpu.stat 2>/dev/null || true
      grep -E "^(oom|oom_kill) " /sys/fs/cgroup/memory.events 2>/dev/null || true
    ' 2>/dev/null | one_line
  )"
  workflow_proc="$(
    docker exec ai-resume-workflow sh -lc '
      grep -E "^(VmRSS|VmHWM|Threads):" /proc/1/status
      printf "open_fds:%s\n" "$(ls /proc/1/fd 2>/dev/null | wc -l)"
      grep -E "^(nr_throttled|throttled_usec) " /sys/fs/cgroup/cpu.stat 2>/dev/null || true
      grep -E "^(oom|oom_kill) " /sys/fs/cgroup/memory.events 2>/dev/null || true
    ' 2>/dev/null | one_line
  )"
  workflow_health="$(
    docker exec ai-resume-workflow sh -lc \
      'curl -fsS --max-time 3 http://127.0.0.1:8090/health' \
      2>/dev/null | one_line
  )"
  task_queue="$(
    docker exec ai-resume-backend sh -lc \
      'curl -fsS --max-time 3 http://127.0.0.1:8080/api/task-queue/status' \
      2>/dev/null | one_line
  )"
  run_queue="$(
    docker exec ai-resume-backend sh -lc \
      'curl -fsS --max-time 3 http://127.0.0.1:8080/api/runs/queue/status' \
      2>/dev/null | one_line
  )"
  run_permits="$(
    docker exec resumai-mysql sh -lc '
      mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "
        SELECT COUNT(*),
               SUM(global_permit_id IS NOT NULL),
               SUM(global_permit_id IS NULL)
        FROM agent_run
        WHERE status IN ('\''STARTING'\'', '\''RUNNING'\'', '\''PAUSING'\'', '\''CANCELLING'\'');"
    ' 2>/dev/null | one_line
  )"
  mysql_status="$(
    docker exec resumai-mysql sh -lc '
      mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" -e "
        SHOW GLOBAL STATUS WHERE Variable_name IN (
          '\''Threads_connected'\'', '\''Threads_running'\'',
          '\''Max_used_connections'\'', '\''Innodb_row_lock_current_waits'\'',
          '\''Innodb_row_lock_time'\'', '\''Slow_queries'\''
        );" 2>/dev/null
    ' 2>/dev/null | one_line
  )"
  redis_status="$(
    docker exec resumai-redis sh -lc '
      redis-cli -a "$REDIS_PASSWORD" --no-auth-warning INFO memory stats clients
    ' 2>/dev/null \
      | grep -E '^(used_memory_human|mem_fragmentation_ratio|instantaneous_ops_per_sec|connected_clients|blocked_clients|evicted_keys):' \
      | one_line
  )"
  postgres_status="$(
    docker exec resumai-langgraph-postgres sh -lc '
      psql -U resumai -d resumai_checkpoint -At -F, -c "
        SELECT numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
               temp_files, temp_bytes, deadlocks,
               pg_database_size('\''resumai_checkpoint'\'')
        FROM pg_stat_database
        WHERE datname = '\''resumai_checkpoint'\'';"
    ' 2>/dev/null | one_line
  )"
  postgres_checkpoints="$(
    docker exec resumai-langgraph-postgres sh -lc '
      psql -U resumai -d resumai_checkpoint -At -F, -c "
        SELECT (SELECT count(*) FROM checkpoints),
               (SELECT count(*) FROM checkpoint_writes),
               (SELECT count(*) FROM checkpoint_blobs);"
    ' 2>/dev/null | one_line
  )"
  disk_status="$(df -P / /data 2>/dev/null | tail -n +2 | one_line)"
  printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
    "$timestamp" "$docker_stats" "$container_state" "$backend_proc" \
    "$workflow_proc" "$workflow_health" "$task_queue" "$run_queue" \
    "$run_permits" "$mysql_status" "$redis_status" "$postgres_status" \
    "$postgres_checkpoints" "$disk_status" >> "$OUT_FILE"
  cycle_elapsed="$(( $(date +%s) - cycle_started ))"
  cycle_remaining="$(( INTERVAL_SECONDS - cycle_elapsed ))"
  if [ "$cycle_remaining" -gt 0 ]; then
    sleep "$cycle_remaining"
  fi
done
