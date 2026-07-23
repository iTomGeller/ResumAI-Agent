-- V19: conversation_turn — lightweight turns for DIRECT_REPLY / BACKGROUND_QUERY.
-- These are NOT agent_run rows and must not enter Policy reward.

-- @guard table:conversation_turn
CREATE TABLE IF NOT EXISTS `conversation_turn` (
  `turn_id`            VARCHAR(64)  NOT NULL PRIMARY KEY,
  `conversation_id`    VARCHAR(64)  NOT NULL,
  `client_message_id`  VARCHAR(128) NOT NULL,
  `disposition`        VARCHAR(32)  NOT NULL,
  `intent`             VARCHAR(64)  NOT NULL,
  `status`             VARCHAR(32)  NOT NULL COMMENT 'PENDING/STREAMING/COMPLETED/FAILED',
  `content`            MEDIUMTEXT   NOT NULL,
  `answer`             MEDIUMTEXT   NULL,
  `citations`          JSON         NULL,
  `actions`            JSON         NULL,
  `error`              VARCHAR(1000) NULL,
  `created_at`         DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `finished_at`        DATETIME(3)  NULL,
  UNIQUE KEY `uk_conversation_turn_client` (`conversation_id`, `client_message_id`),
  KEY `idx_conversation_turn_conv` (`conversation_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Copilot lightweight turns (not agent_run)';
