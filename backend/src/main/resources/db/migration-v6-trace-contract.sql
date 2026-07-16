-- 增量迁移 v6：Trace 契约字段补齐

SET @db := DATABASE();

SET @sql := IF(
  (SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'event_id') < 191,
  'ALTER TABLE `agent_execution_trace` MODIFY COLUMN `event_id` VARCHAR(191) NULL COMMENT ''LangGraph stable event id''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'parent_event_id') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `parent_event_id` VARCHAR(191) NULL COMMENT ''Parent LangGraph event id''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'call_kind') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `call_kind` VARCHAR(32) NULL COMMENT ''llm/tool/mcp/skill/final/node''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'call_name') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `call_name` VARCHAR(128) NULL COMMENT ''Model/tool/skill call name''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'round_role') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `round_role` VARCHAR(32) NULL COMMENT ''decision/tool_result/final/node_start/node_end''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'parent_round_id') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `parent_round_id` VARCHAR(128) NULL COMMENT ''nodeId#roundIndex''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'started_at') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `started_at` DATETIME(3) NULL COMMENT ''Workflow event start time''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'ended_at') = 0,
  'ALTER TABLE `agent_execution_trace` ADD COLUMN `ended_at` DATETIME(3) NULL COMMENT ''Workflow event end time''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND INDEX_NAME = 'idx_agent_trace_parent_event') = 0,
  'CREATE INDEX `idx_agent_trace_parent_event` ON `agent_execution_trace` (`trace_id`, `parent_event_id`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND INDEX_NAME = 'idx_agent_trace_kind_time') = 0,
  'CREATE INDEX `idx_agent_trace_kind_time` ON `agent_execution_trace` (`trace_id`, `event_kind`, `create_time`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
