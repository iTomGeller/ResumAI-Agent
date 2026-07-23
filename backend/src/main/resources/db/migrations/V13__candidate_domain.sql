-- Candidate domain: unique people (candidate_profile) + hiring pipelines
-- (candidate_application), linked from resume_task assessments.

-- @guard table:candidate_profile
CREATE TABLE IF NOT EXISTS `candidate_profile` (
  `id`                 BIGINT       NOT NULL PRIMARY KEY COMMENT '候选人主键',
  `tenant_id`          VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '租户',
  `display_name`       VARCHAR(128) NULL     COMMENT '展示姓名',
  `email`              VARCHAR(256) NULL     COMMENT '邮箱',
  `phone`              VARCHAR(64)  NULL     COMMENT '电话',
  `identity_key`       VARCHAR(128) NOT NULL COMMENT '去重身份键 email:/phone:/name:/hash:',
  `identity_source`    VARCHAR(32)  NOT NULL DEFAULT 'HASH' COMMENT 'EMAIL/PHONE/NAME/HASH',
  `resume_fingerprint` VARCHAR(64)  NULL     COMMENT '正文指纹（SHA-256 hex 前缀）',
  `create_time`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`            TINYINT      NOT NULL DEFAULT 0,
  UNIQUE KEY `uk_candidate_profile_identity` (`tenant_id`, `identity_key`),
  KEY `idx_candidate_profile_email` (`tenant_id`, `email`),
  KEY `idx_candidate_profile_phone` (`tenant_id`, `phone`),
  KEY `idx_candidate_profile_name` (`tenant_id`, `display_name`),
  KEY `idx_candidate_profile_time` (`create_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '候选人档案（一人一行）';

-- @guard table:candidate_application
CREATE TABLE IF NOT EXISTS `candidate_application` (
  `id`                   BIGINT       NOT NULL PRIMARY KEY COMMENT '投递/申请主键',
  `candidate_id`         BIGINT       NOT NULL COMMENT '候选人 ID',
  `tenant_id`            VARCHAR(64)  NOT NULL DEFAULT 'default',
  `job_category`         VARCHAR(64)  NULL,
  `job_id`               VARCHAR(64)  NULL,
  `stage`                VARCHAR(32)  NOT NULL DEFAULT 'NEW' COMMENT 'NEW/SCREENING/INTERVIEW/OFFER/REJECTED/HIRED',
  `owner_hr_id`          VARCHAR(128) NULL     COMMENT '负责 HR',
  `latest_task_id`       BIGINT       NULL     COMMENT '最近一次评估任务',
  `latest_trace_id`      VARCHAR(64)  NULL,
  `latest_score`         INT          NULL,
  `latest_recommendation` VARCHAR(32) NULL,
  `source_file_name`     VARCHAR(256) NULL,
  `create_time`          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`              TINYINT      NOT NULL DEFAULT 0,
  KEY `idx_candidate_app_candidate` (`candidate_id`, `create_time`),
  KEY `idx_candidate_app_stage` (`tenant_id`, `stage`, `update_time`),
  KEY `idx_candidate_app_owner` (`owner_hr_id`, `update_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '候选人投递/申请';

-- @guard column:resume_task.candidate_id
ALTER TABLE `resume_task` ADD COLUMN `candidate_id` BIGINT NULL COMMENT '关联候选人' AFTER `candidate_name`;

-- @guard column:resume_task.application_id
ALTER TABLE `resume_task` ADD COLUMN `application_id` BIGINT NULL COMMENT '关联投递申请' AFTER `candidate_id`;

-- @guard index:resume_task.idx_resume_task_candidate
ALTER TABLE `resume_task` ADD INDEX `idx_resume_task_candidate` (`candidate_id`);

-- @guard index:resume_task.idx_resume_task_application
ALTER TABLE `resume_task` ADD INDEX `idx_resume_task_application` (`application_id`);
