-- 增量迁移 v3：列表查询列化 + 对象存储 key + 索引优化（兼容 MySQL 8.0）

SET @db := DATABASE();

-- resume_task 列表字段
SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'resume_task' AND COLUMN_NAME = 'file_name') = 0,
  'ALTER TABLE `resume_task`
     ADD COLUMN `file_name` VARCHAR(256) NULL COMMENT ''简历文件名'' AFTER `candidate_name`,
     ADD COLUMN `overall_score` INT NULL COMMENT ''综合评分'' AFTER `file_name`,
     ADD COLUMN `recommendation` VARCHAR(32) NULL COMMENT ''推荐结论'' AFTER `overall_score`,
     ADD COLUMN `matched_jd_title` VARCHAR(256) NULL COMMENT ''匹配岗位标题'' AFTER `recommendation`,
     ADD COLUMN `jd_match_score` DECIMAL(5,3) NULL COMMENT ''JD 匹配分'' AFTER `matched_jd_title`,
     ADD COLUMN `duration_ms` BIGINT NULL COMMENT ''评估耗时毫秒'' AFTER `jd_match_score`,
     ADD COLUMN `token_cost` INT NULL COMMENT ''Token 成本'' AFTER `duration_ms`,
     ADD COLUMN `summary` VARCHAR(2000) NULL COMMENT ''评估摘要'' AFTER `token_cost`,
     ADD COLUMN `resume_object_key` VARCHAR(512) NULL COMMENT ''简历对象存储 key'' AFTER `file_url`',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'resume_task' AND INDEX_NAME = 'idx_resume_task_list') = 0,
  'ALTER TABLE `resume_task`
     ADD KEY `idx_resume_task_list` (`create_time`, `status`, `recommendation`, `overall_score`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- llm_invocation 对象存储 key
SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'llm_invocation' AND COLUMN_NAME = 'prompt_object_key') = 0,
  'ALTER TABLE `llm_invocation`
     ADD COLUMN `prompt_object_key` VARCHAR(512) NULL COMMENT ''Prompt 对象存储 key'' AFTER `response_full`,
     ADD COLUMN `response_object_key` VARCHAR(512) NULL COMMENT ''Response 对象存储 key'' AFTER `prompt_object_key`',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 从 result_payload 回填列表字段（已有数据）
UPDATE `resume_task`
SET
  `file_name` = COALESCE(`file_name`, JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.fileName')), `candidate_name`),
  `overall_score` = COALESCE(`overall_score`, CAST(JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.overallScore')) AS SIGNED)),
  `recommendation` = COALESCE(`recommendation`, JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.recommendation'))),
  `matched_jd_title` = COALESCE(`matched_jd_title`, JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.matchedJdTitle'))),
  `jd_match_score` = COALESCE(`jd_match_score`, CAST(JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.jdMatchScore')) AS DECIMAL(5,3))),
  `duration_ms` = COALESCE(`duration_ms`, CAST(JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.durationMs')) AS SIGNED)),
  `token_cost` = COALESCE(`token_cost`, CAST(JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.tokenCost')) AS SIGNED)),
  `summary` = COALESCE(`summary`, LEFT(JSON_UNQUOTE(JSON_EXTRACT(`result_payload`, '$.summary')), 2000))
WHERE `result_payload` IS NOT NULL;
