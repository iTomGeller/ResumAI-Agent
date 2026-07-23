-- V14: Policy Optimization Lab（无 GPU）— minimal experiment / candidate / trial schema.
-- Full isolation (runner/evaluator sandboxes, promotion workflow) may land later;
-- these tables are the durable truth source beyond policy_evolution_log alone.
-- MODEL_WEIGHTS remain unchanged; ONLINE_SELECTION is champion-only in production.

-- @guard table:policy_experiment
CREATE TABLE IF NOT EXISTS `policy_experiment` (
    `experiment_id`         VARCHAR(64)  NOT NULL PRIMARY KEY,
    `kind`                  VARCHAR(32)  NOT NULL DEFAULT 'OFFLINE_SEARCH'
        COMMENT 'ONLINE_SELECTION / OFFLINE_SEARCH / SANDBOX',
    `status`                VARCHAR(32)  NOT NULL DEFAULT 'PENDING'
        COMMENT 'PENDING/RUNNING/COMPLETED/FAILED/CANCELLED',
    `generation`            INT          NOT NULL DEFAULT 0,
    `champion_policy_id`    VARCHAR(64)  NULL,
    `train_dataset_hash`    VARCHAR(64)  NULL,
    `gate_dataset_hash`     VARCHAR(64)  NULL,
    `safety_dataset_hash`   VARCHAR(64)  NULL,
    `code_sha`              VARCHAR(64)  NULL,
    `budget_cny`            DECIMAL(10,4) NULL,
    `spent_cny`             DECIMAL(10,4) NULL DEFAULT 0,
    `created_by`            VARCHAR(128) NULL,
    `started_at`            DATETIME     NULL,
    `finished_at`           DATETIME     NULL,
    `error`                 TEXT         NULL,
    `create_time`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY `idx_policy_experiment_status` (`status`, `generation`),
    KEY `idx_policy_experiment_champion` (`champion_policy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Policy Lab experiment (generation-scoped)';

-- @guard table:policy_candidate
CREATE TABLE IF NOT EXISTS `policy_candidate` (
    `candidate_id`          VARCHAR(64)  NOT NULL PRIMARY KEY,
    `experiment_id`         VARCHAR(64)  NOT NULL,
    `parent_policy_id`      VARCHAR(64)  NULL,
    `config_json`           MEDIUMTEXT   NULL,
    `config_hash`           VARCHAR(64)  NULL,
    `mutation_reason`       TEXT         NULL,
    `status`                VARCHAR(32)  NOT NULL DEFAULT 'DRAFT'
        COMMENT 'DRAFT/EVALUATING/PASSED_GATE/REJECTED/PROMOTED/RETIRED',
    `create_time`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY `idx_policy_candidate_experiment` (`experiment_id`, `status`),
    KEY `idx_policy_candidate_parent` (`parent_policy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Policy Lab mutated / proposed candidates';

-- @guard table:policy_trial
CREATE TABLE IF NOT EXISTS `policy_trial` (
    `trial_id`              VARCHAR(64)  NOT NULL PRIMARY KEY,
    `experiment_id`         VARCHAR(64)  NOT NULL,
    `candidate_id`          VARCHAR(64)  NOT NULL,
    `dataset_split`         VARCHAR(32)  NOT NULL DEFAULT 'eval'
        COMMENT 'train/eval/gate/safety',
    `case_id`               VARCHAR(128) NULL,
    `repeat_no`             INT          NOT NULL DEFAULT 1,
    `seed`                  BIGINT       NULL,
    `status`                VARCHAR(32)  NOT NULL DEFAULT 'PENDING'
        COMMENT 'PENDING/RUNNING/SUCCEEDED/PARTIAL_SUCCESS/FAILED/TIMED_OUT',
    `total_reward`          DECIMAL(8,4) NULL,
    `cost_cny`              DECIMAL(10,4) NULL,
    `latency_ms`            INT          NULL,
    `metrics_json`          MEDIUMTEXT   NULL
        COMMENT 'includes timeline_hit / unsupportedClaimRate / evidenceSupport when present',
    `error`                 TEXT         NULL,
    `started_at`            DATETIME     NULL,
    `finished_at`           DATETIME     NULL,
    `create_time`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY `idx_policy_trial_experiment` (`experiment_id`, `dataset_split`),
    KEY `idx_policy_trial_candidate` (`candidate_id`, `status`),
    KEY `idx_policy_trial_case` (`case_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Policy Lab single case×candidate trial';
