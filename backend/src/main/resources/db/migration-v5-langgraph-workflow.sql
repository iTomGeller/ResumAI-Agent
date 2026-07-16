-- 增量迁移 v5：LangGraph workflow trace 幂等字段（checkpoint 由 LangGraph PostgresSaver 管理，不在 MySQL 建表）

SET @db := DATABASE();

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND COLUMN_NAME = 'event_id') = 0,
  'ALTER TABLE `agent_execution_trace`
     ADD COLUMN `event_id` VARCHAR(128) NULL COMMENT ''LangGraph stable event id'',
     ADD COLUMN `node_id` VARCHAR(64) NULL COMMENT ''LangGraph node id'',
     ADD COLUMN `round_index` INT NULL COMMENT ''LLM round index within node attempt'',
     ADD COLUMN `attempt` INT NOT NULL DEFAULT 1 COMMENT ''Node attempt'',
     ADD COLUMN `event_kind` VARCHAR(32) NULL COMMENT ''node/generation/tool'',
     ADD COLUMN `raw_input` MEDIUMTEXT NULL COMMENT ''Raw LLM messages JSON'',
     ADD COLUMN `raw_output` MEDIUMTEXT NULL COMMENT ''Raw LLM output JSON''',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND INDEX_NAME = 'uk_agent_trace_event_id') = 0,
  'CREATE UNIQUE INDEX `uk_agent_trace_event_id` ON `agent_execution_trace` (`event_id`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'agent_execution_trace' AND INDEX_NAME = 'idx_agent_trace_node_round') = 0,
  'CREATE INDEX `idx_agent_trace_node_round` ON `agent_execution_trace` (`trace_id`, `node_id`, `attempt`, `round_index`, `event_kind`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
