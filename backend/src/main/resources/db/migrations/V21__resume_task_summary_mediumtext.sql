-- V21: evaluation summaries can exceed the legacy 2,000-character ceiling.
-- Keep the full audited result summary instead of relying on non-strict MySQL
-- truncation during writes or disaster recovery.

ALTER TABLE `resume_task`
  MODIFY COLUMN `summary` MEDIUMTEXT NULL COMMENT '评估摘要';
