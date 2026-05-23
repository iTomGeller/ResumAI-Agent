-- ResumAI Agent 全栈 DDL，覆盖 PRD 第 4.1 节定义的 7 张核心表。
-- MySQL 容器首次启动时由 docker-entrypoint-initdb.d 自动执行。

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- 1. 简历评估任务
CREATE TABLE IF NOT EXISTS `resume_task` (
  `id`             BIGINT       NOT NULL PRIMARY KEY COMMENT '任务主键',
  `file_url`       VARCHAR(512) NULL     COMMENT '简历文件地址',
  `job_id`         VARCHAR(64)  NULL     COMMENT '岗位唯一标识',
  `job_category`   VARCHAR(64)  NULL     COMMENT '岗位类别',
  `execution_mode` VARCHAR(32)  NULL     COMMENT '执行模式：SERIAL/DAG_CONCURRENT',
  `status`         VARCHAR(32)  NULL     COMMENT '任务状态',
  `trace_id`       VARCHAR(64)  NOT NULL COMMENT '全局链路追踪 ID',
  `candidate_name` VARCHAR(128) NULL     COMMENT '候选人姓名',
  `fail_reason`    VARCHAR(1024) NULL    COMMENT '失败原因',
  `start_time`     DATETIME     NULL     COMMENT '任务开始时间',
  `end_time`       DATETIME     NULL     COMMENT '任务结束时间',
  `create_time`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`        TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  UNIQUE KEY `uk_resume_task_trace_id` (`trace_id`),
  KEY `idx_resume_task_status` (`status`),
  KEY `idx_resume_task_job_category` (`job_category`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '简历评估任务';

-- 2. Agent 执行链路
CREATE TABLE IF NOT EXISTS `agent_execution_trace` (
  `id`              BIGINT       NOT NULL PRIMARY KEY COMMENT '主键',
  `trace_id`        VARCHAR(64)  NOT NULL COMMENT '全局链路追踪 ID',
  `span_id`         VARCHAR(64)  NOT NULL COMMENT 'Span ID',
  `parent_span_id`  VARCHAR(64)  NULL     COMMENT '父 Span ID',
  `agent_role`      VARCHAR(64)  NULL     COMMENT 'Agent 角色',
  `skill_name`      VARCHAR(128) NULL     COMMENT '挂载 Skill 名称',
  `tool_call`       VARCHAR(128) NULL     COMMENT '工具调用名称',
  `rag_strategy`    VARCHAR(64)  NULL     COMMENT 'RAG 策略',
  `model_name`      VARCHAR(64)  NULL     COMMENT '模型名称',
  `input_summary`   VARCHAR(2000) NULL    COMMENT '输入摘要',
  `output_summary`  VARCHAR(2000) NULL    COMMENT '输出摘要',
  `payload`         JSON         NULL     COMMENT '结构化调用载荷',
  `duration_ms`     BIGINT       NULL     COMMENT '耗时毫秒',
  `cost_tokens`     BIGINT       NULL     COMMENT 'Token 成本',
  `retry_count`     INT          NULL     DEFAULT 0 COMMENT '重试次数',
  `status`          VARCHAR(32)  NULL     COMMENT '执行状态',
  `error_message`   VARCHAR(2000) NULL    COMMENT '异常信息',
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`         TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  KEY `idx_trace_create_time` (`trace_id`, `create_time`),
  KEY `idx_agent_role` (`agent_role`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Agent 执行链路';

-- 3. 动态 Skill Prompt
CREATE TABLE IF NOT EXISTS `dynamic_skill_prompt` (
  `id`              BIGINT       NOT NULL PRIMARY KEY COMMENT '主键',
  `skill_name`      VARCHAR(128) NOT NULL COMMENT 'Skill 名称',
  `prompt_template` TEXT         NOT NULL COMMENT 'Prompt 模板',
  `version`         INT          NOT NULL DEFAULT 1 COMMENT '版本',
  `enabled`         TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用',
  `description`     VARCHAR(512) NULL     COMMENT '说明',
  `created_by`      VARCHAR(64)  NULL     COMMENT '创建人',
  `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`         TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  KEY `idx_skill_name_version` (`skill_name`, `version`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '动态 Skill Prompt';

-- 4. 人工反馈日志
CREATE TABLE IF NOT EXISTS `human_feedback_log` (
  `id`             BIGINT       NOT NULL PRIMARY KEY COMMENT '主键',
  `trace_id`       VARCHAR(64)  NOT NULL COMMENT '全局链路追踪 ID',
  `report_id`      VARCHAR(64)  NULL     COMMENT '报告 ID',
  `rating_score`   INT          NULL     COMMENT '人工评分',
  `human_comment`  VARCHAR(2000) NULL    COMMENT '人工批注',
  `fix_action`     VARCHAR(2000) NULL    COMMENT '修正动作',
  `feedback_type`  VARCHAR(32)  NULL     COMMENT '反馈类型',
  `reviewer`       VARCHAR(128) NULL     COMMENT '反馈人',
  `adopted`        TINYINT      NULL     DEFAULT 0 COMMENT '是否采纳',
  `create_time`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`        TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  KEY `idx_feedback_trace` (`trace_id`),
  KEY `idx_feedback_type_time` (`feedback_type`, `create_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '人工反馈日志';

-- 5. Meta-Agent 自进化历史
CREATE TABLE IF NOT EXISTS `meta_evolution_history` (
  `id`              BIGINT        NOT NULL PRIMARY KEY COMMENT '主键',
  `trace_id`        VARCHAR(64)   NULL     COMMENT '触发进化的 Trace ID',
  `evolution_type`  VARCHAR(32)   NOT NULL COMMENT '进化类型',
  `target_table`    VARCHAR(64)   NOT NULL COMMENT '目标表',
  `target_id`       BIGINT        NULL     COMMENT '目标记录 ID',
  `before_value`    JSON          NULL     COMMENT '修改前值',
  `after_value`     JSON          NULL     COMMENT '修改后值',
  `reason`          VARCHAR(2000) NULL     COMMENT '修改原因',
  `risk_level`      VARCHAR(16)   NULL     COMMENT '风险等级',
  `approval_status` VARCHAR(32)   NOT NULL DEFAULT 'PENDING' COMMENT '审核状态',
  `rollback_data`   JSON          NULL     COMMENT '回滚数据',
  `create_time`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`         TINYINT       NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  KEY `idx_evolution_status` (`approval_status`),
  KEY `idx_evolution_target` (`target_table`, `target_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'Meta-Agent 自进化历史';

-- 6. RAGAS 评估指标
CREATE TABLE IF NOT EXISTS `ragas_eval_metrics` (
  `id`                BIGINT        NOT NULL PRIMARY KEY COMMENT '主键',
  `trace_id`          VARCHAR(64)   NOT NULL COMMENT '全局链路追踪 ID',
  `span_id`           VARCHAR(64)   NOT NULL COMMENT '被评估 Span ID',
  `context_precision` DECIMAL(5,3)  NULL     COMMENT '上下文精确率',
  `context_recall`    DECIMAL(5,3)  NULL     COMMENT '上下文召回率',
  `faithfulness`      DECIMAL(5,3)  NULL     COMMENT '事实一致性',
  `answer_relevancy`  DECIMAL(5,3)  NULL     COMMENT '回答相关性',
  `overall_score`     DECIMAL(5,3)  NULL     COMMENT '综合评分',
  `passed`            TINYINT       NULL     DEFAULT 1 COMMENT '是否通过阈值',
  `judge_reason`      VARCHAR(2000) NULL     COMMENT '裁判原因',
  `create_time`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`           TINYINT       NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  KEY `idx_ragas_trace_span` (`trace_id`, `span_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'RAGAS 评估指标';

-- 7. 系统宏观调度规则
CREATE TABLE IF NOT EXISTS `system_orchestration_rule` (
  `id`                       BIGINT        NOT NULL PRIMARY KEY COMMENT '主键',
  `job_category`             VARCHAR(64)   NOT NULL COMMENT '岗位类别',
  `required_agents`          JSON          NULL     COMMENT '必需 Agent 列表',
  `preferred_rag_strategy`   VARCHAR(64)   NULL     COMMENT '首选 RAG 策略',
  `top_k`                    INT           NULL     DEFAULT 5 COMMENT '检索 TopK',
  `max_retry`                INT           NULL     DEFAULT 2 COMMENT '最大重试次数',
  `faithfulness_threshold`   DECIMAL(5,3)  NULL     DEFAULT 0.800 COMMENT '事实一致性阈值',
  `execution_policy`         VARCHAR(32)   NULL     COMMENT '执行策略',
  `enabled`                  TINYINT       NOT NULL DEFAULT 1 COMMENT '是否启用',
  `version`                  INT           NOT NULL DEFAULT 1 COMMENT '版本',
  `create_time`              DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`              DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`                  TINYINT       NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  UNIQUE KEY `uk_rule_job_category_version` (`job_category`, `version`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '系统宏观调度规则';

-- 默认 Skill Prompt（提供给 Agent 即开即用的模板）
INSERT INTO `dynamic_skill_prompt` (`id`, `skill_name`, `prompt_template`, `version`, `enabled`, `description`, `created_by`)
VALUES
  (1, 'TechStackAuditSkill',
   '请基于岗位与简历上下文评估候选人的技术栈匹配度，输出关键证据、缺口与改进建议。',
   1, 1, '技术能力评估默认 Prompt', 'system'),
  (2, 'ProjectDepthSkill',
   '请评估候选人项目的复杂度、个人贡献、技术深度与业务价值，并提示需要面试官追问的盲点。',
   1, 1, '项目深度评估默认 Prompt', 'system'),
  (3, 'RiskDetectionSkill',
   '请扫描简历的时间线一致性、夸大表述、堆砌关键词与潜在合规风险并给出复核建议。',
   1, 1, '风险识别默认 Prompt', 'system')
ON DUPLICATE KEY UPDATE `prompt_template` = VALUES(`prompt_template`);

-- 默认调度规则（保证 Orchestrator 启动时已有 TECH / PRODUCT / DESIGN 三套基线）
INSERT INTO `system_orchestration_rule`
  (`id`, `job_category`, `required_agents`, `preferred_rag_strategy`,
   `top_k`, `max_retry`, `faithfulness_threshold`, `execution_policy`, `enabled`, `version`)
VALUES
  (1, 'TECH',
   JSON_ARRAY('ResumeParserAgent','TechAgent','ProjectAgent','RiskAgent','RagasJudgeAgent','FinalReportAgent'),
   'HYBRID', 6, 2, 0.800, 'DAG_CONCURRENT', 1, 1),
  (2, 'PRODUCT',
   JSON_ARRAY('ResumeParserAgent','TechAgent','ProjectAgent','RiskAgent','RagasJudgeAgent','FinalReportAgent'),
   'GRAPHRAG', 5, 2, 0.800, 'SERIAL', 1, 1),
  (3, 'DESIGN',
   JSON_ARRAY('ResumeParserAgent','ProjectAgent','RiskAgent','RagasJudgeAgent','FinalReportAgent'),
   'VECTOR', 5, 2, 0.750, 'SERIAL', 1, 1)
ON DUPLICATE KEY UPDATE `required_agents` = VALUES(`required_agents`);

SET FOREIGN_KEY_CHECKS = 1;
