-- Raise low_cost LLM budget so dynamic full_evaluation can still finish ReportAgent.
UPDATE policy_bundle
SET config = JSON_SET(config, '$.maxLlmCalls', 12, '$.maxAgentCount', 6),
    update_time = NOW()
WHERE policy_id = 'low_cost';
