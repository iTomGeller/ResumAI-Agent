-- V16: candidate domain backfill support — origin tagging, link status,
-- identity confidence, application reuse key, and idempotent ledger.

-- @guard column:resume_task.data_origin
ALTER TABLE `resume_task`
  ADD COLUMN `data_origin` VARCHAR(32) NOT NULL DEFAULT 'USER_UPLOAD'
  COMMENT 'USER_UPLOAD/BENCHMARK/ACCEPTANCE/SYSTEM' AFTER `application_id`;

-- @guard column:resume_task.candidate_link_status
ALTER TABLE `resume_task`
  ADD COLUMN `candidate_link_status` VARCHAR(32) NULL
  COMMENT 'LINKED/SKIPPED/FAILED/PENDING' AFTER `data_origin`;

-- @guard column:resume_task.candidate_link_reason
ALTER TABLE `resume_task`
  ADD COLUMN `candidate_link_reason` VARCHAR(256) NULL
  COMMENT 'link/skip reason' AFTER `candidate_link_status`;

-- @guard column:candidate_profile.identity_confidence
ALTER TABLE `candidate_profile`
  ADD COLUMN `identity_confidence` DECIMAL(5,3) NOT NULL DEFAULT 1.000
  COMMENT '0-1 identity confidence' AFTER `resume_fingerprint`;

-- @guard column:candidate_profile.needs_merge_review
ALTER TABLE `candidate_profile`
  ADD COLUMN `needs_merge_review` TINYINT NOT NULL DEFAULT 0
  COMMENT '1=可能需人工合并复核' AFTER `identity_confidence`;

-- @guard column:candidate_profile.data_origin
ALTER TABLE `candidate_profile`
  ADD COLUMN `data_origin` VARCHAR(32) NOT NULL DEFAULT 'USER_UPLOAD'
  COMMENT 'USER_UPLOAD/BENCHMARK/ACCEPTANCE/SYSTEM' AFTER `needs_merge_review`;

-- @guard column:candidate_application.application_key
ALTER TABLE `candidate_application`
  ADD COLUMN `application_key` VARCHAR(191) NULL
  COMMENT 'tenant-scoped reuse key: candidateId:normalizedJob' AFTER `job_id`;

-- @guard index:candidate_application.uk_candidate_application_key
ALTER TABLE `candidate_application`
  ADD UNIQUE KEY `uk_candidate_application_key` (`tenant_id`, `application_key`);

-- @guard table:candidate_backfill_ledger
CREATE TABLE IF NOT EXISTS `candidate_backfill_ledger` (
  `task_id`         BIGINT       NOT NULL PRIMARY KEY COMMENT 'resume_task.id',
  `candidate_id`    BIGINT       NULL,
  `application_id`  BIGINT       NULL,
  `identity_key`    VARCHAR(191) NULL,
  `action`          VARCHAR(32)  NOT NULL COMMENT 'LINKED/SKIPPED/FAILED/REUSED',
  `error`           VARCHAR(1000) NULL,
  `migrated_at`     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY `idx_backfill_ledger_candidate` (`candidate_id`),
  KEY `idx_backfill_ledger_action` (`action`, `migrated_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '候选人历史回填幂等账本';
