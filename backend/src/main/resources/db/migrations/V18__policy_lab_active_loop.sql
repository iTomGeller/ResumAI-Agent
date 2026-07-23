-- V18: Policy Lab active loop — extend V14 and add promotion/metrics/events.
-- Source of truth is DB (not reports/evolution/*.json). auto_promote defaults to 0.

-- @guard column:policy_experiment.config_json
ALTER TABLE `policy_experiment`
  ADD COLUMN `config_json` MEDIUMTEXT NULL AFTER `error`,
  ADD COLUMN `eval_dataset` VARCHAR(64) NULL AFTER `config_json`,
  ADD COLUMN `gate_dataset` VARCHAR(64) NULL AFTER `eval_dataset`,
  ADD COLUMN `safety_dataset` VARCHAR(64) NULL AFTER `gate_dataset`,
  ADD COLUMN `seeds_json` JSON NULL AFTER `safety_dataset`,
  ADD COLUMN `repeats_per_case` INT NOT NULL DEFAULT 1 AFTER `seeds_json`,
  ADD COLUMN `case_limit` INT NULL AFTER `repeats_per_case`,
  ADD COLUMN `run_type` VARCHAR(64) NULL AFTER `case_limit`,
  ADD COLUMN `cohort_key` VARCHAR(128) NOT NULL DEFAULT 'default' AFTER `run_type`,
  ADD COLUMN `base_policy_id` VARCHAR(64) NULL AFTER `cohort_key`,
  ADD COLUMN `progress_pct` DECIMAL(5,2) NOT NULL DEFAULT 0 AFTER `base_policy_id`,
  ADD COLUMN `progress_phase` VARCHAR(32) NULL AFTER `progress_pct`,
  ADD COLUMN `pause_requested` TINYINT NOT NULL DEFAULT 0 AFTER `progress_phase`,
  ADD COLUMN `cancel_requested` TINYINT NOT NULL DEFAULT 0 AFTER `pause_requested`,
  ADD COLUMN `auto_promote` TINYINT NOT NULL DEFAULT 0 AFTER `cancel_requested`,
  ADD COLUMN `runner_image_digest` VARCHAR(128) NULL AFTER `auto_promote`,
  ADD COLUMN `evaluator_image_digest` VARCHAR(128) NULL AFTER `runner_image_digest`,
  ADD COLUMN `result_json` MEDIUMTEXT NULL AFTER `evaluator_image_digest`,
  ADD COLUMN `note` VARCHAR(512) NULL AFTER `result_json`;

-- Update status comment to include PAUSED
ALTER TABLE `policy_experiment`
  MODIFY COLUMN `status` VARCHAR(32) NOT NULL DEFAULT 'PENDING'
    COMMENT 'PENDING/RUNNING/PAUSED/CANCELLED/COMPLETED/FAILED';

-- @guard column:policy_candidate.bundle_policy_id
ALTER TABLE `policy_candidate`
  ADD COLUMN `bundle_policy_id` VARCHAR(64) NULL AFTER `parent_policy_id`,
  ADD COLUMN `mutation_patch` MEDIUMTEXT NULL AFTER `config_hash`,
  ADD COLUMN `reflector_model` VARCHAR(128) NULL AFTER `mutation_patch`,
  ADD COLUMN `gate_metrics_json` MEDIUMTEXT NULL AFTER `mutation_reason`;

-- Unique config hash per experiment (ignore NULLs via separate key if needed)
-- MySQL allows multiple NULLs in UNIQUE; config_hash should be set for real candidates.
ALTER TABLE `policy_candidate`
  ADD UNIQUE KEY `uk_policy_candidate_config` (`experiment_id`, `config_hash`);

-- @guard column:policy_trial.run_id
ALTER TABLE `policy_trial`
  ADD COLUMN `run_id` VARCHAR(64) NULL AFTER `seed`,
  ADD COLUMN `trajectory_uri` VARCHAR(512) NULL AFTER `run_id`,
  ADD COLUMN `result_uri` VARCHAR(512) NULL AFTER `trajectory_uri`,
  ADD COLUMN `runner_sandbox_id` VARCHAR(96) NULL AFTER `result_uri`,
  ADD COLUMN `evaluator_sandbox_id` VARCHAR(96) NULL AFTER `runner_sandbox_id`,
  ADD COLUMN `reward_components_json` MEDIUMTEXT NULL AFTER `metrics_json`,
  ADD KEY `idx_policy_trial_run` (`run_id`);

-- @guard column:sandbox_execution.purpose
ALTER TABLE `sandbox_execution`
  ADD COLUMN `purpose` VARCHAR(32) NOT NULL DEFAULT 'LEGACY_CANDIDATE_EVALUATION'
    COMMENT 'POLICY_EVOLUTION/BENCHMARK/REPLAY/LEGACY_CANDIDATE_EVALUATION' AFTER `status`,
  ADD COLUMN `experiment_id` VARCHAR(64) NULL AFTER `purpose`,
  ADD COLUMN `trial_id` VARCHAR(64) NULL AFTER `experiment_id`,
  ADD KEY `idx_sandbox_experiment` (`experiment_id`, `create_time`);

-- @guard table:policy_metric
CREATE TABLE IF NOT EXISTS `policy_metric` (
  `id`            BIGINT AUTO_INCREMENT PRIMARY KEY,
  `trial_id`      VARCHAR(64)   NOT NULL,
  `metric_name`   VARCHAR(128)  NOT NULL,
  `metric_value`  DECIMAL(16,6) NULL,
  `metric_status` VARCHAR(32)   NOT NULL,
  `detail_json`   MEDIUMTEXT    NULL,
  KEY `idx_policy_metric_trial` (`trial_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Per-trial metric rows for Policy Lab';

-- @guard table:policy_promotion
CREATE TABLE IF NOT EXISTS `policy_promotion` (
  `id`                   BIGINT AUTO_INCREMENT PRIMARY KEY,
  `experiment_id`        VARCHAR(64)  NOT NULL,
  `candidate_id`         VARCHAR(64)  NOT NULL,
  `run_type`             VARCHAR(64)  NOT NULL,
  `cohort_key`           VARCHAR(128) NOT NULL DEFAULT 'default',
  `previous_policy_id`   VARCHAR(64)  NULL,
  `promoted_policy_id`   VARCHAR(64)  NULL,
  `hard_gates_json`      MEDIUMTEXT   NOT NULL,
  `metric_deltas_json`   MEDIUMTEXT   NOT NULL,
  `confidence_json`      MEDIUMTEXT   NOT NULL,
  `decision`             VARCHAR(32)  NOT NULL COMMENT 'PROMOTE/ROLLBACK/REJECT',
  `decided_by`           VARCHAR(128) NOT NULL,
  `decided_at`           DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `reason`               VARCHAR(512) NULL,
  KEY `idx_policy_promotion_exp` (`experiment_id`, `decided_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Manual Policy Lab promote/rollback ledger';

-- @guard table:policy_experiment_event
CREATE TABLE IF NOT EXISTS `policy_experiment_event` (
  `id`             BIGINT AUTO_INCREMENT PRIMARY KEY,
  `experiment_id`  VARCHAR(64)  NOT NULL,
  `seq`            INT          NOT NULL,
  `event_type`     VARCHAR(64)  NOT NULL,
  `payload`        MEDIUMTEXT   NULL,
  `create_time`    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY `uk_policy_experiment_seq` (`experiment_id`, `seq`),
  KEY `idx_policy_experiment_event_time` (`experiment_id`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Policy Lab experiment SSE event log';
