-- 一次性修复：陈旧 RUNNING（Worker 已不在执行，但 queue_status 仍为 RUNNING）
UPDATE `resume_task`
SET
  `queue_status` = 'RETRYING',
  `status` = 'QUEUED',
  `worker_id` = NULL,
  `next_retry_at` = NOW(),
  `update_time` = NOW()
WHERE `deleted` = 0
  AND `queue_status` = 'RUNNING'
  AND (`started_at` IS NULL OR `started_at` < DATE_SUB(NOW(), INTERVAL 30 MINUTE));
