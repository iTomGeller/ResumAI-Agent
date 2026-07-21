-- V9: plan-approval mode flag on agent_run — the run pauses right after the
-- Coordinator plan and waits for user confirmation before burning budget.

-- @guard column:agent_run.plan_mode
ALTER TABLE `agent_run` ADD COLUMN `plan_mode` TINYINT NOT NULL DEFAULT 0
    COMMENT '1=规划后暂停等待用户确认计划' AFTER `queue_mode`;
