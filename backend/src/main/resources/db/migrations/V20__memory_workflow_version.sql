-- V20: version identity for trustworthy Memory TTL cohorts.

-- @guard column:memory_entry.producer_version
ALTER TABLE `memory_entry`
  ADD COLUMN `producer_version` VARCHAR(64) NULL AFTER `version`;

-- @guard column:run_memory_usage.consumer_version
ALTER TABLE `run_memory_usage`
  ADD COLUMN `consumer_version` VARCHAR(64) NULL AFTER `consumer_agent`;

-- @guard index:idx_memory_producer_version
CREATE INDEX `idx_memory_producer_version`
  ON `memory_entry` (`producer_version`, `create_time`);

-- @guard index:idx_memory_usage_consumer_version
CREATE INDEX `idx_memory_usage_consumer_version`
  ON `run_memory_usage` (`consumer_version`, `create_time`);
