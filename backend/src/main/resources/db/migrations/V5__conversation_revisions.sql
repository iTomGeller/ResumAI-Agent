-- V5: bring a pre-conversation database up to the conversational-revision schema.
-- Every statement is guarded so re-running (or running on a freshly
-- bootstrapped schema.sql database) is a no-op.

-- @guard column:resume_task.resume_text
ALTER TABLE `resume_task` ADD COLUMN `resume_text` MEDIUMTEXT NULL COMMENT '不可变简历输入快照' AFTER `resume_object_key`;

-- @guard column:resume_task.job_description
ALTER TABLE `resume_task` ADD COLUMN `job_description` MEDIUMTEXT NULL COMMENT '不可变 JD 输入快照' AFTER `job_category`;

-- @guard column:resume_task.evaluation_brief
ALTER TABLE `resume_task` ADD COLUMN `evaluation_brief` MEDIUMTEXT NULL COMMENT '本 revision 的评估重点/补充上下文' AFTER `job_description`;

-- @guard column:resume_task.invalidated_nodes
ALTER TABLE `resume_task` ADD COLUMN `invalidated_nodes` JSON NULL COMMENT '需要重跑的 LangGraph 节点' AFTER `evaluation_brief`;

-- @guard column:resume_task.rag_options
ALTER TABLE `resume_task` ADD COLUMN `rag_options` JSON NULL COMMENT 'RAG 选项快照' AFTER `invalidated_nodes`;

-- @guard column:resume_task.conversation_id
ALTER TABLE `resume_task` ADD COLUMN `conversation_id` VARCHAR(64) NULL COMMENT '持续对话 ID' AFTER `trace_id`;

-- @guard column:resume_task.revision_no
ALTER TABLE `resume_task` ADD COLUMN `revision_no` INT NOT NULL DEFAULT 1 COMMENT '会话内不可变版本号' AFTER `conversation_id`;

-- @guard column:resume_task.workflow_run_id
ALTER TABLE `resume_task` ADD COLUMN `workflow_run_id` VARCHAR(64) NULL COMMENT 'Python runtime run ID' AFTER `revision_no`;

-- @guard column:resume_task.base_workflow_run_id
ALTER TABLE `resume_task` ADD COLUMN `base_workflow_run_id` VARCHAR(64) NULL COMMENT '复用 checkpoint 的来源 run ID' AFTER `workflow_run_id`;

-- @guard column:resume_task.supersedes_trace_id
ALTER TABLE `resume_task` ADD COLUMN `supersedes_trace_id` VARCHAR(64) NULL COMMENT '被当前版本替代的 Trace ID' AFTER `base_workflow_run_id`;

-- @guard column:resume_task.superseded_by_trace_id
ALTER TABLE `resume_task` ADD COLUMN `superseded_by_trace_id` VARCHAR(64) NULL COMMENT '替代当前版本的 Trace ID' AFTER `supersedes_trace_id`;

-- @guard index:resume_task.uk_resume_task_conversation_revision
ALTER TABLE `resume_task` ADD UNIQUE KEY `uk_resume_task_conversation_revision` (`conversation_id`, `revision_no`);

-- @guard table:conversation_session
CREATE TABLE IF NOT EXISTS `conversation_session` (
  `id`              VARCHAR(64)  NOT NULL PRIMARY KEY,
  `active_trace_id` VARCHAR(64)  NOT NULL,
  `active_revision` INT          NOT NULL DEFAULT 1,
  `tenant_id`       VARCHAR(64)  NOT NULL DEFAULT 'default',
  `created_by`      VARCHAR(128) NULL,
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`         TINYINT      NOT NULL DEFAULT 0,
  KEY `idx_conversation_active_trace` (`active_trace_id`),
  KEY `idx_conversation_tenant_time` (`tenant_id`, `update_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '简历评估持续对话';

-- @guard table:conversation_message
CREATE TABLE IF NOT EXISTS `conversation_message` (
  `id`                BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `conversation_id`   VARCHAR(64)   NOT NULL,
  `client_message_id` VARCHAR(191)  NOT NULL,
  `role`              VARCHAR(16)   NOT NULL,
  `intent_type`       VARCHAR(64)   NULL,
  `content`           MEDIUMTEXT    NOT NULL,
  `revision_no`       INT           NOT NULL DEFAULT 1,
  `metadata_json`     JSON          NULL,
  `create_time`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `deleted`           TINYINT       NOT NULL DEFAULT 0,
  UNIQUE KEY `uk_conversation_client_message` (`conversation_id`, `client_message_id`),
  KEY `idx_conversation_message_order` (`conversation_id`, `id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '简历评估会话消息';
