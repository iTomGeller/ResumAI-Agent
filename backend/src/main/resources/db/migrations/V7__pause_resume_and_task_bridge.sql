-- V7: pause/resume snapshots on agent_run and the legacy resume_task bridge.
-- Guard-aware and re-runnable; never drops tables or data.

-- @guard column:agent_run.pause_reason
ALTER TABLE `agent_run` ADD COLUMN `pause_reason` VARCHAR(500) NULL COMMENT '暂停原因' AFTER `cancellation_reason`;

-- @guard column:agent_run.execution_snapshot
ALTER TABLE `agent_run` ADD COLUMN `execution_snapshot` MEDIUMTEXT NULL COMMENT '安全边界执行快照(JSON)' AFTER `pause_reason`;

-- @guard column:agent_run.source_task_trace_id
ALTER TABLE `agent_run` ADD COLUMN `source_task_trace_id` VARCHAR(64) NULL COMMENT '关联的 resume_task traceId' AFTER `execution_snapshot`;

-- @guard index:agent_run.idx_agent_run_source_task
ALTER TABLE `agent_run` ADD INDEX `idx_agent_run_source_task` (`source_task_trace_id`);
