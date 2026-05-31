-- 增量迁移：评估结果快照 + LLM 完整调用记录（兼容 MySQL 8.0）

SET @db := DATABASE();

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'resume_task' AND COLUMN_NAME = 'result_payload') = 0,
  'ALTER TABLE `resume_task` ADD COLUMN `result_payload` JSON NULL COMMENT ''评估结果快照'' AFTER `fail_reason`',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS `llm_invocation` (
  `id`                 VARCHAR(64)  NOT NULL PRIMARY KEY COMMENT '调用 ID',
  `trace_id`           VARCHAR(64)  NULL     COMMENT 'Trace ID',
  `span_id`            VARCHAR(64)  NULL     COMMENT 'Span ID',
  `model_name`         VARCHAR(64)  NULL     COMMENT '模型名称',
  `agent_role`         VARCHAR(64)  NULL     COMMENT 'Agent 角色',
  `purpose`            VARCHAR(64)  NULL     COMMENT '调用目的',
  `request_started_at` DATETIME     NULL     COMMENT '请求开始时间',
  `duration_ms`        BIGINT       NULL     COMMENT '耗时毫秒',
  `input_tokens`       INT          NULL     COMMENT '输入 Token',
  `output_tokens`      INT          NULL     COMMENT '输出 Token',
  `finish_reason`      VARCHAR(32)  NULL     COMMENT '结束原因',
  `truncated`          TINYINT      NULL     DEFAULT 0 COMMENT '是否截断',
  `prompt_chars`       INT          NULL     COMMENT 'Prompt 字符数',
  `response_chars`     INT          NULL     COMMENT 'Response 字符数',
  `prompt_preview`     VARCHAR(2000) NULL    COMMENT 'Prompt 预览',
  `response_preview`   VARCHAR(2000) NULL    COMMENT 'Response 预览',
  `prompt_full`        MEDIUMTEXT   NULL     COMMENT '完整 Prompt',
  `response_full`      MEDIUMTEXT   NULL     COMMENT '完整 Response',
  `error_code`         VARCHAR(64)  NULL     COMMENT '错误码',
  `error_body`         VARCHAR(2000) NULL    COMMENT '错误体',
  `create_time`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`            TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  KEY `idx_llm_trace` (`trace_id`),
  KEY `idx_llm_agent` (`agent_role`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'LLM 完整调用记录';

CREATE TABLE IF NOT EXISTS `jd_library` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
  `jd_id`       VARCHAR(64)  NOT NULL COMMENT '岗位唯一标识（前端生成）',
  `title`       VARCHAR(256) NOT NULL COMMENT '岗位标题',
  `category`    VARCHAR(64)  NULL     COMMENT '岗位类别',
  `description` TEXT         NULL     COMMENT '岗位描述全文',
  `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted`     TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除',
  UNIQUE KEY `uk_jd_library_jd_id` (`jd_id`),
  KEY `idx_jd_library_category` (`category`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT 'JD 向量库';
