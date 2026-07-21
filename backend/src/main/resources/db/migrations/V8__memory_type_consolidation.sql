-- V8: consolidate memory types 7 -> 4 (CONVERSATION / EPISODIC / PREFERENCE / FAILURE).
-- Table is memory_entry (V6). UPDATEs are no-ops when no matching rows exist.
-- No @guard table: — that guard skips when the table EXISTS (meant for CREATE).

UPDATE `memory_entry` SET `type` = 'PREFERENCE', `update_time` = NOW()
WHERE `type` = 'USER_PREFERENCE';

UPDATE `memory_entry` SET `type` = 'PREFERENCE', `update_time` = NOW()
WHERE `type` = 'HR_FEEDBACK';

UPDATE `memory_entry` SET `status` = 'ARCHIVED', `update_time` = NOW()
WHERE `type` IN ('WORKING', 'DOMAIN') AND `status` = 'ACTIVE';
