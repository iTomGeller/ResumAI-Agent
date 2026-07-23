-- V15: widen policy_selection.selection_mode and introduce per-runType champion
-- assignment so CHAMPION_FALLBACK / FALLBACK never truncates and low_cost cannot
-- remain the production champion for full_evaluation.

-- @guard column:policy_selection.selection_mode
ALTER TABLE `policy_selection`
  MODIFY COLUMN `selection_mode` VARCHAR(32) NOT NULL
  COMMENT 'CHAMPION/FALLBACK/EXPLORE/EXPLOIT/THOMPSON/FORCED';

-- @guard table:policy_champion_assignment
CREATE TABLE IF NOT EXISTS `policy_champion_assignment` (
  `id`             BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `run_type`       VARCHAR(64)  NOT NULL,
  `cohort_key`     VARCHAR(128) NOT NULL DEFAULT 'default',
  `policy_id`      VARCHAR(64)  NOT NULL,
  `experiment_id`  VARCHAR(64)  NULL,
  `approved_by`    VARCHAR(128) NOT NULL,
  `approved_at`    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version`        INT          NOT NULL DEFAULT 1,
  `active`         TINYINT      NOT NULL DEFAULT 1,
  UNIQUE KEY `uk_policy_champion_scope` (`run_type`, `cohort_key`, `active`),
  KEY `idx_policy_champion_policy` (`policy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Per runType/cohort champion assignment';

UPDATE `policy_bundle` SET `is_champion` = 0 WHERE `policy_id` = 'low_cost';
UPDATE `policy_bundle` SET `is_champion` = 1 WHERE `policy_id` = 'balanced';

INSERT INTO `policy_champion_assignment`
  (`run_type`, `cohort_key`, `policy_id`, `approved_by`)
VALUES
  ('full_evaluation', 'default', 'balanced', 'migration-v15'),
  ('jd_evaluation', 'default', 'balanced', 'migration-v15'),
  ('backend_eval', 'default', 'backend_job', 'migration-v15'),
  ('agent_eval', 'default', 'agent_job', 'migration-v15')
ON DUPLICATE KEY UPDATE
  `policy_id` = VALUES(`policy_id`),
  `version` = `version` + 1,
  `approved_by` = VALUES(`approved_by`);
