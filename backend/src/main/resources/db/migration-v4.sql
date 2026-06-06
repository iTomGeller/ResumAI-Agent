-- 增量迁移 v4：任务队列字段 + JD 乐观锁（兼容 MySQL 8.0）

SET @db := DATABASE();

-- resume_task 队列调度字段
SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'resume_task' AND COLUMN_NAME = 'uploaded_by') = 0,
  'ALTER TABLE `resume_task`
     ADD COLUMN `uploaded_by` VARCHAR(128) NULL COMMENT ''上传 HR 标识'' AFTER `candidate_name`,
     ADD COLUMN `tenant_id` VARCHAR(64) NULL DEFAULT ''default'' COMMENT ''租户标识'' AFTER `uploaded_by`,
     ADD COLUMN `priority` INT NOT NULL DEFAULT 0 COMMENT ''任务优先级'' AFTER `tenant_id`,
     ADD COLUMN `queue_status` VARCHAR(32) NULL DEFAULT ''QUEUED'' COMMENT ''队列状态'' AFTER `status`,
     ADD COLUMN `queued_at` DATETIME NULL COMMENT ''入队时间'' AFTER `queue_status`,
     ADD COLUMN `started_at` DATETIME NULL COMMENT ''开始消费时间'' AFTER `queued_at`,
     ADD COLUMN `finished_at` DATETIME NULL COMMENT ''结束时间'' AFTER `started_at`,
     ADD COLUMN `attempt_count` INT NOT NULL DEFAULT 0 COMMENT ''已重试次数'' AFTER `finished_at`,
     ADD COLUMN `next_retry_at` DATETIME NULL COMMENT ''下次重试时间'' AFTER `attempt_count`,
     ADD COLUMN `worker_id` VARCHAR(128) NULL COMMENT ''消费 worker 标识'' AFTER `next_retry_at`',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'resume_task' AND INDEX_NAME = 'idx_resume_task_queue') = 0,
  'ALTER TABLE `resume_task`
     ADD KEY `idx_resume_task_queue` (`queue_status`, `priority`, `queued_at`),
     ADD KEY `idx_resume_task_uploaded_by` (`uploaded_by`, `create_time`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE `resume_task`
SET
  `queue_status` = CASE
    WHEN `status` = 'SUCCESS' THEN 'SUCCESS'
    WHEN `status` = 'FAILED' THEN 'FAILED'
    WHEN `status` = 'RUNNING' THEN 'RETRYING'
    WHEN `queue_status` IS NULL THEN 'QUEUED'
    ELSE `queue_status`
  END,
  `queued_at` = COALESCE(`queued_at`, `create_time`),
  `finished_at` = CASE
    WHEN `status` IN ('SUCCESS', 'FAILED') THEN COALESCE(`finished_at`, `update_time`, `create_time`)
    ELSE `finished_at`
  END,
  `tenant_id` = COALESCE(`tenant_id`, 'default'),
  `uploaded_by` = COALESCE(`uploaded_by`, 'legacy')
WHERE `queue_status` IS NULL
   OR `queue_status` = 'QUEUED'
   OR `queued_at` IS NULL
   OR `tenant_id` IS NULL
   OR `uploaded_by` IS NULL;

-- jd_library 乐观锁字段
SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'jd_library' AND COLUMN_NAME = 'version') = 0,
  'ALTER TABLE `jd_library`
     ADD COLUMN `version` INT NOT NULL DEFAULT 1 COMMENT ''乐观锁版本'' AFTER `description`,
     ADD COLUMN `updated_by` VARCHAR(128) NULL COMMENT ''最后修改 HR'' AFTER `version`,
     ADD COLUMN `tenant_id` VARCHAR(64) NULL DEFAULT ''default'' COMMENT ''租户标识'' AFTER `updated_by`',
  'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE `jd_library`
SET
  `version` = COALESCE(`version`, 1),
  `tenant_id` = COALESCE(`tenant_id`, 'default')
WHERE `version` IS NULL OR `tenant_id` IS NULL;
