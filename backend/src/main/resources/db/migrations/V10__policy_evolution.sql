-- V10: Policy Optimization Lab OFFLINE_SEARCH audit trail (bounded evolutionary
-- search over policy text/parameters, no GPU; not full GEPA): lineage columns on
-- policy_bundle and a full audit log of every generation / promotion / retirement.

-- @guard column:policy_bundle.parent_policy_id
ALTER TABLE `policy_bundle` ADD COLUMN `parent_policy_id` VARCHAR(64) NULL
    COMMENT '变异来源策略（谱系追溯）' AFTER `is_champion`;

-- @guard column:policy_bundle.generation
ALTER TABLE `policy_bundle` ADD COLUMN `generation` INT NOT NULL DEFAULT 0
    COMMENT '进化代数（0=人工种子）' AFTER `parent_policy_id`;

-- @guard table:policy_evolution_log
CREATE TABLE `policy_evolution_log` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `generation` INT NOT NULL COMMENT '进化代数',
    `policy_id` VARCHAR(64) NOT NULL,
    `parent_policy_id` VARCHAR(64) NULL,
    `action` VARCHAR(32) NOT NULL COMMENT 'CANDIDATE_CREATED / PROMOTED / RETIRED / REJECTED',
    `mutation_reason` TEXT NULL COMMENT 'LLM 反思变异依据（来自失败 trace）',
    `benchmark_score` DECIMAL(8,4) NULL COMMENT 'held-out 基准得分',
    `champion_score` DECIMAL(8,4) NULL COMMENT '同轮 champion 得分（对照）',
    `detail` TEXT NULL,
    `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_evolution_policy` (`policy_id`),
    INDEX `idx_evolution_generation` (`generation`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='策略进化审计日志';
