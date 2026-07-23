-- V17: Ops debug indexes + run_memory_usage evidence table
-- Supports run-centric timeline drilldown and memory used/ignored decisions.

-- @guard index:idx_run_event_type_time
CREATE INDEX `idx_run_event_type_time`
  ON `run_event` (`event_type`, `create_time`);

-- @guard index:idx_run_event_tool_time
CREATE INDEX `idx_run_event_tool_time`
  ON `run_event` (`tool_name`, `create_time`);

-- @guard index:idx_memory_run_time
CREATE INDEX `idx_memory_run_time`
  ON `memory_entry` (`run_id`, `update_time`);

-- @guard table:run_memory_usage
CREATE TABLE IF NOT EXISTS `run_memory_usage` (
  `id`              BIGINT        NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_id`          VARCHAR(64)   NOT NULL,
  `memory_id`       VARCHAR(64)   NOT NULL,
  `consumer_agent`  VARCHAR(64)   NOT NULL,
  `rank_no`         INT           NULL,
  `vector_score`    DECIMAL(8,5)  NULL,
  `lexical_score`   DECIMAL(8,5)  NULL,
  `recency_score`   DECIMAL(8,5)  NULL,
  `final_score`     DECIMAL(8,5)  NULL,
  `decision`        VARCHAR(16)   NOT NULL COMMENT 'USED/IGNORED',
  `ignored_reason`  VARCHAR(256)  NULL,
  `create_time`     DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY `idx_run_memory_usage_run` (`run_id`, `consumer_agent`),
  KEY `idx_run_memory_usage_memory` (`memory_id`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Per-run memory retrieval used/ignored evidence';
