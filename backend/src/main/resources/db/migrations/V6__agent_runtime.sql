-- V6: conversational agent runtime — users, runs, run events, executions,
-- tool calls, layered memory, context snapshots, sandbox executions,
-- policy learning and benchmark storage.
-- All statements are guarded / IF NOT EXISTS so the file is re-runnable.

-- @guard table:app_user
CREATE TABLE IF NOT EXISTS `app_user` (
  `id`          VARCHAR(64)  NOT NULL PRIMARY KEY COMMENT '用户 ID',
  `display_name` VARCHAR(128) NULL,
  `role`        VARCHAR(32)  NOT NULL DEFAULT 'HR',
  `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`     TINYINT      NOT NULL DEFAULT 0
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '平台用户';

INSERT INTO `app_user` (`id`, `display_name`, `role`) VALUES
  ('demo-hr', 'Demo HR', 'HR'),
  ('bench-runner', 'Benchmark Runner', 'SYSTEM')
ON DUPLICATE KEY UPDATE `display_name` = VALUES(`display_name`);

-- @guard column:conversation_session.user_id
ALTER TABLE `conversation_session` ADD COLUMN `user_id` VARCHAR(64) NOT NULL DEFAULT 'demo-hr' COMMENT '所属用户' AFTER `id`;

-- @guard column:conversation_session.title
ALTER TABLE `conversation_session` ADD COLUMN `title` VARCHAR(256) NULL AFTER `user_id`;

-- @guard column:conversation_session.resume_text
ALTER TABLE `conversation_session` ADD COLUMN `resume_text` MEDIUMTEXT NULL COMMENT '会话简历快照' AFTER `title`;

-- @guard column:conversation_session.job_description
ALTER TABLE `conversation_session` ADD COLUMN `job_description` MEDIUMTEXT NULL COMMENT '会话 JD 快照' AFTER `resume_text`;

-- @guard column:conversation_session.job_category
ALTER TABLE `conversation_session` ADD COLUMN `job_category` VARCHAR(64) NULL AFTER `job_description`;

-- @guard column:conversation_session.summary
ALTER TABLE `conversation_session` ADD COLUMN `summary` MEDIUMTEXT NULL COMMENT '会话结构化摘要' AFTER `job_category`;

-- @guard column:conversation_session.summary_version
ALTER TABLE `conversation_session` ADD COLUMN `summary_version` INT NOT NULL DEFAULT 0 AFTER `summary`;

-- @guard column:conversation_session.current_goal
ALTER TABLE `conversation_session` ADD COLUMN `current_goal` VARCHAR(2000) NULL COMMENT '用户当前目标' AFTER `summary_version`;

-- @guard column:conversation_message.run_id
ALTER TABLE `conversation_message` ADD COLUMN `run_id` VARCHAR(64) NULL COMMENT '触发/产生该消息的 Run' AFTER `revision_no`;

-- @guard column:conversation_message.queue_mode
ALTER TABLE `conversation_message` ADD COLUMN `queue_mode` VARCHAR(16) NULL COMMENT 'collect/interrupt' AFTER `run_id`;

-- @guard table:agent_run
CREATE TABLE IF NOT EXISTS `agent_run` (
  `run_id`            VARCHAR(64)  NOT NULL PRIMARY KEY,
  `conversation_id`   VARCHAR(64)  NOT NULL,
  `user_id`           VARCHAR(64)  NOT NULL DEFAULT 'demo-hr',
  `trace_id`          VARCHAR(64)  NOT NULL,
  `revision_no`       INT          NOT NULL DEFAULT 1,
  `run_type`          VARCHAR(64)  NULL COMMENT '任务类别：full_evaluation/tech_match/...',
  `queue_mode`        VARCHAR(16)  NOT NULL DEFAULT 'collect',
  `user_message`      MEDIUMTEXT   NULL,
  `merged_message_ids` JSON        NULL COMMENT '被合并进本 Run 的消息 ID',
  `status`            VARCHAR(32)  NOT NULL DEFAULT 'QUEUED',
  `current_agent`     VARCHAR(64)  NULL,
  `current_tool`      VARCHAR(128) NULL,
  `current_phase`     VARCHAR(64)  NULL COMMENT 'llm/tool/sandbox/plan',
  `answer`            MEDIUMTEXT   NULL,
  `shared_state`      JSON         NULL,
  `metrics`           JSON         NULL COMMENT 'llmCalls/toolCalls/tokens/costs',
  `policy_id`         VARCHAR(64)  NULL,
  `prompt_versions`   JSON         NULL,
  `skill_versions`    JSON         NULL,
  `retry_count`       INT          NOT NULL DEFAULT 0,
  `error_code`        VARCHAR(64)  NULL,
  `error_message`     VARCHAR(2000) NULL,
  `cancellation_reason` VARCHAR(512) NULL,
  `conv_permit_id`    VARCHAR(128) NULL,
  `global_permit_id`  VARCHAR(128) NULL,
  `created_at`        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `started_at`        DATETIME(3)  NULL,
  `updated_at`        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `finished_at`       DATETIME(3)  NULL,
  `timeout_at`        DATETIME(3)  NULL,
  `deleted`           TINYINT      NOT NULL DEFAULT 0,
  KEY `idx_agent_run_conv_order` (`conversation_id`, `created_at`),
  KEY `idx_agent_run_status` (`status`, `created_at`),
  KEY `idx_agent_run_user` (`user_id`, `created_at`),
  KEY `idx_agent_run_trace` (`trace_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '对话 Agent Run';

-- @guard table:run_event
CREATE TABLE IF NOT EXISTS `run_event` (
  `id`              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_id`          VARCHAR(64)  NOT NULL,
  `conversation_id` VARCHAR(64)  NOT NULL,
  `trace_id`        VARCHAR(64)  NULL,
  `seq`             INT          NOT NULL,
  `event_type`      VARCHAR(64)  NOT NULL,
  `agent_id`        VARCHAR(64)  NULL,
  `tool_name`       VARCHAR(128) NULL,
  `payload`         JSON         NULL,
  `create_time`     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY `uk_run_event_seq` (`run_id`, `seq`),
  KEY `idx_run_event_conv` (`conversation_id`, `id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Run 流式事件（SSE 回放源）';

-- @guard table:agent_execution
CREATE TABLE IF NOT EXISTS `agent_execution` (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_id`       VARCHAR(64)  NOT NULL,
  `agent_id`     VARCHAR(64)  NOT NULL,
  `status`       VARCHAR(32)  NOT NULL DEFAULT 'RUNNING',
  `iterations`   INT          NOT NULL DEFAULT 0,
  `llm_calls`    INT          NOT NULL DEFAULT 0,
  `tool_calls`   INT          NOT NULL DEFAULT 0,
  `output`       JSON         NULL,
  `error_message` VARCHAR(2000) NULL,
  `started_at`   DATETIME(3)  NULL,
  `finished_at`  DATETIME(3)  NULL,
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_agent_execution_run` (`run_id`, `id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '单 Agent 执行记录';

-- @guard table:tool_call_log
CREATE TABLE IF NOT EXISTS `tool_call_log` (
  `tool_call_id`   VARCHAR(96)  NOT NULL PRIMARY KEY,
  `run_id`         VARCHAR(64)  NOT NULL,
  `agent_id`       VARCHAR(64)  NULL,
  `tool_name`      VARCHAR(128) NOT NULL,
  `arguments`      JSON         NULL,
  `result_preview` VARCHAR(4000) NULL,
  `status`         VARCHAR(32)  NOT NULL DEFAULT 'RUNNING',
  `error`          VARCHAR(2000) NULL,
  `retry_count`    INT          NOT NULL DEFAULT 0,
  `duration_ms`    BIGINT       NULL,
  `progress`       VARCHAR(256) NULL,
  `heartbeat_at`   DATETIME(3)  NULL,
  `idempotency_key` VARCHAR(191) NULL,
  `side_effect_level` VARCHAR(32) NULL,
  `started_at`     DATETIME(3)  NULL,
  `finished_at`    DATETIME(3)  NULL,
  KEY `idx_tool_call_run` (`run_id`, `started_at`),
  KEY `idx_tool_call_idem` (`idempotency_key`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Tool 调用与结果记录';

-- @guard table:memory_entry
CREATE TABLE IF NOT EXISTS `memory_entry` (
  `memory_id`       VARCHAR(64)  NOT NULL PRIMARY KEY,
  `type`            VARCHAR(32)  NOT NULL COMMENT 'WORKING/CONVERSATION/EPISODIC/USER_PREFERENCE/HR_FEEDBACK/DOMAIN/FAILURE',
  `owner_scope`     VARCHAR(32)  NOT NULL COMMENT 'RUN/CONVERSATION/USER/GLOBAL',
  `user_id`         VARCHAR(64)  NULL,
  `conversation_id` VARCHAR(64)  NULL,
  `run_id`          VARCHAR(64)  NULL,
  `content`         MEDIUMTEXT   NOT NULL,
  `structured_content` JSON      NULL,
  `content_hash`    VARCHAR(64)  NOT NULL,
  `source`          VARCHAR(64)  NOT NULL DEFAULT 'agent' COMMENT 'user_confirmed/system_rule/model_generated/hr_feedback/tool_result',
  `source_id`       VARCHAR(128) NULL,
  `confidence`      DECIMAL(5,3) NOT NULL DEFAULT 0.500,
  `status`          VARCHAR(32)  NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/ARCHIVED/CONFLICTED/DELETED/EXPIRED',
  `version`         INT          NOT NULL DEFAULT 1,
  `embedding`       JSON         NULL,
  `sensitivity_level` VARCHAR(16) NOT NULL DEFAULT 'NORMAL',
  `expires_at`      DATETIME     NULL,
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY `idx_memory_scope` (`owner_scope`, `user_id`, `conversation_id`, `run_id`, `status`),
  KEY `idx_memory_type_time` (`type`, `update_time`),
  KEY `idx_memory_hash` (`content_hash`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '分层 Agent Memory';

-- @guard table:context_snapshot
CREATE TABLE IF NOT EXISTS `context_snapshot` (
  `id`                       BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_id`                   VARCHAR(64) NOT NULL,
  `conversation_id`          VARCHAR(64) NOT NULL,
  `summary_version`          INT         NOT NULL DEFAULT 1,
  `source_message_start_id`  BIGINT      NULL,
  `source_message_end_id`    BIGINT      NULL,
  `first_kept_message_id`    BIGINT      NULL,
  `before_token_estimate`    INT         NULL,
  `after_token_estimate`     INT         NULL,
  `reason`                   VARCHAR(256) NULL,
  `summary`                  MEDIUMTEXT  NULL,
  `create_time`              DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY `idx_context_snapshot_run` (`run_id`, `id`),
  KEY `idx_context_snapshot_conv` (`conversation_id`, `id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Context 压缩快照';

-- @guard table:sandbox_execution
CREATE TABLE IF NOT EXISTS `sandbox_execution` (
  `id`              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `sandbox_id`      VARCHAR(96)  NOT NULL,
  `run_id`          VARCHAR(64)  NOT NULL,
  `conversation_id` VARCHAR(64)  NULL,
  `tool_name`       VARCHAR(128) NOT NULL,
  `container_id`    VARCHAR(96)  NULL,
  `status`          VARCHAR(32)  NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING/SUCCEEDED/FAILED/TIMED_OUT/OOM_KILLED/CANCELLED',
  `exit_code`       INT          NULL,
  `duration_ms`     BIGINT       NULL,
  `stdout_tail`     VARCHAR(4000) NULL,
  `stderr_tail`     VARCHAR(2000) NULL,
  `error`           VARCHAR(2000) NULL,
  `expire_at`       DATETIME     NULL,
  `create_time`     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `finished_at`     DATETIME(3)  NULL,
  UNIQUE KEY `uk_sandbox_execution` (`sandbox_id`),
  KEY `idx_sandbox_run` (`run_id`, `id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Sandbox 执行记录';

-- @guard table:policy_bundle
CREATE TABLE IF NOT EXISTS `policy_bundle` (
  `policy_id`   VARCHAR(64)  NOT NULL PRIMARY KEY,
  `name`        VARCHAR(128) NOT NULL,
  `description` VARCHAR(1000) NULL,
  `config`      JSON         NOT NULL,
  `status`      VARCHAR(32)  NOT NULL DEFAULT 'ACTIVE',
  `is_champion` TINYINT      NOT NULL DEFAULT 0,
  `version`     INT          NOT NULL DEFAULT 1,
  `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Agent 外层策略包';

-- @guard table:policy_selection
CREATE TABLE IF NOT EXISTS `policy_selection` (
  `id`            BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_id`        VARCHAR(64) NOT NULL,
  `policy_id`     VARCHAR(64) NOT NULL,
  `task_category` VARCHAR(64) NOT NULL,
  `selection_mode` VARCHAR(16) NOT NULL COMMENT 'EXPLOIT/EXPLORE/FORCED',
  `epsilon`       DECIMAL(5,3) NULL,
  `context`       JSON        NULL,
  `create_time`   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_policy_selection_run` (`run_id`),
  KEY `idx_policy_selection_policy` (`policy_id`, `task_category`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '策略选择记录';

-- @guard table:policy_statistics
CREATE TABLE IF NOT EXISTS `policy_statistics` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `policy_id`     VARCHAR(64)  NOT NULL,
  `task_category` VARCHAR(64)  NOT NULL,
  `run_count`     INT          NOT NULL DEFAULT 0,
  `reward_count`  INT          NOT NULL DEFAULT 0,
  `total_reward`  DECIMAL(12,4) NOT NULL DEFAULT 0,
  `avg_reward`    DECIMAL(8,4) NOT NULL DEFAULT 0,
  `reward_sq_sum` DECIMAL(16,6) NOT NULL DEFAULT 0 COMMENT 'Thompson Sampling 用平方和',
  `success_count` INT          NOT NULL DEFAULT 0,
  `failure_count` INT          NOT NULL DEFAULT 0,
  `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY `uk_policy_stats` (`policy_id`, `task_category`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '策略统计';

-- @guard table:policy_reward
CREATE TABLE IF NOT EXISTS `policy_reward` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_id`        VARCHAR(64)  NOT NULL,
  `policy_id`     VARCHAR(64)  NOT NULL,
  `task_category` VARCHAR(64)  NOT NULL,
  `source`        VARCHAR(32)  NOT NULL COMMENT 'FEEDBACK/BENCHMARK/AUTO',
  `feedback_id`   BIGINT       NULL,
  `total_reward`  DECIMAL(8,4) NOT NULL,
  `components`    JSON         NOT NULL,
  `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_policy_reward_run` (`run_id`),
  KEY `idx_policy_reward_policy` (`policy_id`, `task_category`, `create_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Reward 记录（分量单独保存于 components）';

-- @guard table:benchmark_case
CREATE TABLE IF NOT EXISTS `benchmark_case` (
  `case_id`       VARCHAR(96)  NOT NULL PRIMARY KEY,
  `dataset`       VARCHAR(32)  NOT NULL COMMENT 'GOLD/SYNTHETIC/REGRESSION/SECURITY',
  `resume_text`   MEDIUMTEXT   NULL,
  `jd_text`       MEDIUMTEXT   NULL,
  `user_question` VARCHAR(2000) NULL,
  `must_find`     JSON         NULL,
  `must_not_claim` JSON        NULL,
  `expected_evidence` JSON     NULL,
  `expected_risk` JSON         NULL,
  `metadata`      JSON         NULL,
  `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY `idx_benchmark_case_dataset` (`dataset`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Benchmark 用例';

-- @guard table:benchmark_run
CREATE TABLE IF NOT EXISTS `benchmark_run` (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `benchmark_id` VARCHAR(96)  NOT NULL COMMENT '一次 benchmark 批次',
  `case_id`      VARCHAR(96)  NOT NULL,
  `policy_id`    VARCHAR(64)  NOT NULL,
  `run_id`       VARCHAR(64)  NULL,
  `status`       VARCHAR(32)  NOT NULL DEFAULT 'RUNNING',
  `metrics`      JSON         NULL,
  `report_path`  VARCHAR(512) NULL,
  `started_at`   DATETIME(3)  NULL,
  `finished_at`  DATETIME(3)  NULL,
  KEY `idx_benchmark_run_batch` (`benchmark_id`, `policy_id`),
  KEY `idx_benchmark_run_case` (`case_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Benchmark 运行结果';

-- @guard column:human_feedback_log.run_id
ALTER TABLE `human_feedback_log` ADD COLUMN `run_id` VARCHAR(64) NULL COMMENT '关联 Run' AFTER `trace_id`;

-- @guard column:human_feedback_log.policy_id
ALTER TABLE `human_feedback_log` ADD COLUMN `policy_id` VARCHAR(64) NULL AFTER `run_id`;

-- @guard column:human_feedback_log.structured_payload
ALTER TABLE `human_feedback_log` ADD COLUMN `structured_payload` JSON NULL COMMENT '结构化反馈（分数修正/遗漏证据等）' AFTER `fix_action`;

INSERT INTO `policy_bundle` (`policy_id`, `name`, `description`, `config`, `status`, `version`) VALUES
  ('balanced', '均衡策略', '默认策略：完整流水线、标准预算、启用证据核验',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('JDAnalysisAgent','TechAgent','ProjectAgent','RiskAgent','EvidenceAgent','ReportAgent'),
     'maxAgentCount', 6, 'maxLlmCalls', 12, 'maxIterationsPerAgent', 2,
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 20, 'maxToolCallsPerAgent', 5),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 2200, 'policyBudget', 320, 'skillBudget', 1200, 'recentMessageBudget', 2600, 'memoryBudget', 1500, 'toolResultBudget', 3600, 'reservedOutputBudget', 2048, 'compactAtRatio', 0.75),
     'memoryRetrieval', JSON_OBJECT('topK', 5, 'minConfidence', 0.35),
     'evidenceVerification', JSON_OBJECT('enabled', TRUE, 'strict', FALSE, 'minSupportRatio', 0.5),
     'rewriteRounds', 1,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 900, 'llmTimeoutSeconds', 120, 'toolTimeoutSeconds', 30, 'sandboxTimeoutSeconds', 90)),
   'ACTIVE', 1),
  ('strict_evidence', '严格证据策略', '每个结论强制 Sandbox 证据核验，宁缺毋滥',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('JDAnalysisAgent','TechAgent','ProjectAgent','RiskAgent','EvidenceAgent','ReportAgent'),
     'maxAgentCount', 6, 'maxLlmCalls', 14, 'maxIterationsPerAgent', 2,
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 26, 'maxToolCallsPerAgent', 6),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 2200, 'policyBudget', 320, 'skillBudget', 1200, 'recentMessageBudget', 2200, 'memoryBudget', 1500, 'toolResultBudget', 4200, 'reservedOutputBudget', 2048, 'compactAtRatio', 0.72),
     'memoryRetrieval', JSON_OBJECT('topK', 5, 'minConfidence', 0.5),
     'evidenceVerification', JSON_OBJECT('enabled', TRUE, 'strict', TRUE, 'minSupportRatio', 0.7),
     'rewriteRounds', 1,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 1080, 'llmTimeoutSeconds', 120, 'toolTimeoutSeconds', 30, 'sandboxTimeoutSeconds', 120)),
   'ACTIVE', 1),
  ('deep_analysis', '深度分析策略', '更多迭代与检索预算，追求覆盖率与深度',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('JDAnalysisAgent','TechAgent','ProjectAgent','RiskAgent','EvidenceAgent','ReportAgent'),
     'maxAgentCount', 7, 'maxLlmCalls', 18, 'maxIterationsPerAgent', 3,
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 32, 'maxToolCallsPerAgent', 8),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 2400, 'policyBudget', 320, 'skillBudget', 1600, 'recentMessageBudget', 2600, 'memoryBudget', 2000, 'toolResultBudget', 5200, 'reservedOutputBudget', 2048, 'compactAtRatio', 0.7),
     'memoryRetrieval', JSON_OBJECT('topK', 8, 'minConfidence', 0.3),
     'evidenceVerification', JSON_OBJECT('enabled', TRUE, 'strict', FALSE, 'minSupportRatio', 0.6),
     'rewriteRounds', 2,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 1200, 'llmTimeoutSeconds', 150, 'toolTimeoutSeconds', 40, 'sandboxTimeoutSeconds', 120)),
   'ACTIVE', 1),
  ('low_cost', '低成本策略', '最小 Agent 组合与预算，快速回答',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('TechAgent','ReportAgent'),
     'maxAgentCount', 6, 'maxLlmCalls', 12, 'maxIterationsPerAgent', 1,
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 8, 'maxToolCallsPerAgent', 3),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 1800, 'policyBudget', 240, 'skillBudget', 700, 'recentMessageBudget', 1600, 'memoryBudget', 800, 'toolResultBudget', 2000, 'reservedOutputBudget', 1600, 'compactAtRatio', 0.8),
     'memoryRetrieval', JSON_OBJECT('topK', 3, 'minConfidence', 0.5),
     'evidenceVerification', JSON_OBJECT('enabled', FALSE, 'strict', FALSE, 'minSupportRatio', 0.3),
     'rewriteRounds', 1,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 480, 'llmTimeoutSeconds', 90, 'toolTimeoutSeconds', 20, 'sandboxTimeoutSeconds', 60)),
   'ACTIVE', 1),
  ('backend_job', 'Java 后端岗位策略', '针对后端岗位加权基础/并发/中间件证据与追问',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('JDAnalysisAgent','TechAgent','ProjectAgent','RiskAgent','EvidenceAgent','ReportAgent'),
     'maxAgentCount', 6, 'maxLlmCalls', 13, 'maxIterationsPerAgent', 2,
     'jobFocus', 'java_backend',
     'skillOverrides', JSON_OBJECT('TechAgent', 'java_backend_evaluation'),
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 22, 'maxToolCallsPerAgent', 6),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 2200, 'policyBudget', 360, 'skillBudget', 1400, 'recentMessageBudget', 2400, 'memoryBudget', 1500, 'toolResultBudget', 3800, 'reservedOutputBudget', 2048, 'compactAtRatio', 0.75),
     'memoryRetrieval', JSON_OBJECT('topK', 5, 'minConfidence', 0.4),
     'evidenceVerification', JSON_OBJECT('enabled', TRUE, 'strict', TRUE, 'minSupportRatio', 0.6),
     'rewriteRounds', 1,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 960, 'llmTimeoutSeconds', 120, 'toolTimeoutSeconds', 30, 'sandboxTimeoutSeconds', 90)),
   'ACTIVE', 1),
  ('agent_job', 'AI Agent 岗位策略', '针对 Agent/LLM 岗位加权工程化、评测与落地证据',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('JDAnalysisAgent','TechAgent','ProjectAgent','RiskAgent','EvidenceAgent','ReportAgent'),
     'maxAgentCount', 6, 'maxLlmCalls', 13, 'maxIterationsPerAgent', 2,
     'jobFocus', 'ai_agent',
     'skillOverrides', JSON_OBJECT('TechAgent', 'ai_agent_job_evaluation'),
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 22, 'maxToolCallsPerAgent', 6),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 2200, 'policyBudget', 360, 'skillBudget', 1400, 'recentMessageBudget', 2400, 'memoryBudget', 1500, 'toolResultBudget', 3800, 'reservedOutputBudget', 2048, 'compactAtRatio', 0.75),
     'memoryRetrieval', JSON_OBJECT('topK', 5, 'minConfidence', 0.4),
     'evidenceVerification', JSON_OBJECT('enabled', TRUE, 'strict', TRUE, 'minSupportRatio', 0.6),
     'rewriteRounds', 1,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 960, 'llmTimeoutSeconds', 120, 'toolTimeoutSeconds', 30, 'sandboxTimeoutSeconds', 90)),
   'ACTIVE', 1),
  ('resume_rewrite', '简历改写策略', '项目改写与整体优化专用：改写轮次与 lint 检查',
   JSON_OBJECT(
     'agentOrder', JSON_ARRAY('ProjectAgent','ResumeOptimizeAgent'),
     'maxAgentCount', 4, 'maxLlmCalls', 10, 'maxIterationsPerAgent', 2,
     'toolBudget', JSON_OBJECT('maxToolCallsPerRun', 12, 'maxToolCallsPerAgent', 4),
     'contextBudget', JSON_OBJECT('modelWindow', 65536, 'systemBudget', 2000, 'policyBudget', 280, 'skillBudget', 1200, 'recentMessageBudget', 2400, 'memoryBudget', 1200, 'toolResultBudget', 2600, 'reservedOutputBudget', 2600, 'compactAtRatio', 0.78),
     'memoryRetrieval', JSON_OBJECT('topK', 4, 'minConfidence', 0.4),
     'evidenceVerification', JSON_OBJECT('enabled', TRUE, 'strict', FALSE, 'minSupportRatio', 0.4),
     'rewriteRounds', 2,
     'timeoutPolicy', JSON_OBJECT('runTimeoutSeconds', 720, 'llmTimeoutSeconds', 120, 'toolTimeoutSeconds', 30, 'sandboxTimeoutSeconds', 90)),
   'ACTIVE', 1)
ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);
