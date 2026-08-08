-- Replace the legacy generic memory taxonomy with two job-scoped business
-- layers. Historical rows remain available for audit but are never recalled
-- by the new runtime.

UPDATE `memory_entry`
SET `status` = 'ARCHIVED',
    `update_time` = CURRENT_TIMESTAMP
WHERE `status` = 'ACTIVE'
  AND `type` NOT IN ('RECENT_CASE', 'JOB_PROFILE');

ALTER TABLE `memory_entry`
  MODIFY COLUMN `type` VARCHAR(32) NOT NULL
  COMMENT 'RECENT_CASE(30d)/JOB_PROFILE(180d)';
