-- EvidenceAgent has been removed from the production workflow. Preserve each
-- policy's existing order while deleting only the retired entry and switch.
UPDATE `policy_bundle`
SET `config` = JSON_REMOVE(
    `config`,
    JSON_UNQUOTE(JSON_SEARCH(`config`, 'one', 'EvidenceAgent', NULL, '$.agentOrder[*]'))
)
WHERE JSON_SEARCH(`config`, 'one', 'EvidenceAgent', NULL, '$.agentOrder[*]') IS NOT NULL;

UPDATE `policy_bundle`
SET `config` = JSON_REMOVE(`config`, '$.evidenceVerification')
WHERE JSON_CONTAINS_PATH(`config`, 'one', '$.evidenceVerification');

UPDATE `system_orchestration_rule`
SET `required_agents` = JSON_REMOVE(
    `required_agents`,
    JSON_UNQUOTE(JSON_SEARCH(`required_agents`, 'one', 'EvidenceAgent'))
)
WHERE JSON_SEARCH(`required_agents`, 'one', 'EvidenceAgent') IS NOT NULL;
