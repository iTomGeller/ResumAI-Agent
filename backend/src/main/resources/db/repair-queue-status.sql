-- 一次性修复：历史 SUCCESS/FAILED 任务被错误标记为 queue_status=QUEUED
UPDATE `resume_task`
SET
  `queue_status` = CASE
    WHEN `status` = 'SUCCESS' THEN 'SUCCESS'
    WHEN `status` = 'FAILED' THEN 'FAILED'
    WHEN `status` = 'RUNNING' THEN 'RETRYING'
    ELSE `queue_status`
  END,
  `finished_at` = CASE
    WHEN `status` IN ('SUCCESS', 'FAILED') THEN COALESCE(`finished_at`, `update_time`, `create_time`)
    ELSE `finished_at`
  END,
  `update_time` = NOW()
WHERE `deleted` = 0
  AND `queue_status` = 'QUEUED'
  AND `status` IN ('SUCCESS', 'FAILED', 'RUNNING');
