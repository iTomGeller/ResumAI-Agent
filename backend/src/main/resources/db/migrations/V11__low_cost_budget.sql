-- DEPRECATED by V12__policy_goal_contract.sql.
-- Raising low_cost budget so it could finish full_evaluation was the wrong
-- fix; V12 restores a thin budget and excludes full_evaluation via
-- supportedRunTypes eligibility instead.
UPDATE policy_bundle
SET config = JSON_SET(config, '$.maxLlmCalls', 12, '$.maxAgentCount', 6),
    update_time = NOW()
WHERE policy_id = 'low_cost';