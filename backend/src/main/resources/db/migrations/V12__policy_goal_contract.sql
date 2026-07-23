-- Policy goal contract: run-type eligibility + required/optional artifacts.
-- Replaces the V11 "raise low_cost budget for full_evaluation" workaround:
-- low_cost is no longer eligible for full_evaluation / jd_evaluation / *_eval.

-- low_cost: thin budget, only light Q&A / narrow checks — never full eval.
UPDATE `policy_bundle`
SET `config` = JSON_SET(
        `config`,
        '$.maxLlmCalls', 6,
        '$.maxAgentCount', 3,
        '$.supportedRunTypes', JSON_ARRAY(
            'quick_answer', 'followup', 'tech_match', 'evidence_check',
            'timeline_check', 'risk_check'),
        '$.requiredArtifacts', JSON_ARRAY('final_report'),
        '$.optionalArtifacts', JSON_ARRAY('technical_findings', 'evidence_ledger', 'risks')),
    `description` = '低成本策略：仅轻量问答/窄域检查，不承接 full_evaluation',
    `update_time` = NOW()
WHERE `policy_id` = 'low_cost';

-- Full-pipeline policies: explicit support for evaluation run types.
UPDATE `policy_bundle`
SET `config` = JSON_SET(
        `config`,
        '$.supportedRunTypes', JSON_ARRAY(
            'full_evaluation', 'jd_evaluation', 'backend_eval', 'agent_eval',
            'tech_match', 'project_analysis', 'risk_check', 'timeline_check',
            'evidence_check', 'jd_gap', 'interview_questions', 'followup',
            'quick_answer'),
        '$.requiredArtifacts', JSON_ARRAY(
            'resume_facts', 'jd_requirements', 'technical_findings',
            'project_findings', 'risks', 'evidence_ledger', 'final_report'),
        '$.optionalArtifacts', JSON_ARRAY()),
    `update_time` = NOW()
WHERE `policy_id` IN ('balanced', 'strict_evidence', 'deep_analysis');

UPDATE `policy_bundle`
SET `config` = JSON_SET(
        `config`,
        '$.supportedRunTypes', JSON_ARRAY(
            'full_evaluation', 'jd_evaluation', 'backend_eval', 'agent_eval',
            'tech_match', 'project_analysis', 'risk_check', 'timeline_check',
            'evidence_check', 'jd_gap', 'interview_questions', 'followup',
            'quick_answer'),
        '$.requiredArtifacts', JSON_ARRAY(
            'resume_facts', 'jd_requirements', 'technical_findings',
            'project_findings', 'risks', 'evidence_ledger', 'final_report'),
        '$.optionalArtifacts', JSON_ARRAY(),
        '$.jobFocus', 'java_backend'),
    `update_time` = NOW()
WHERE `policy_id` = 'backend_job';

UPDATE `policy_bundle`
SET `config` = JSON_SET(
        `config`,
        '$.supportedRunTypes', JSON_ARRAY(
            'full_evaluation', 'jd_evaluation', 'backend_eval', 'agent_eval',
            'tech_match', 'project_analysis', 'risk_check', 'timeline_check',
            'evidence_check', 'jd_gap', 'interview_questions', 'followup',
            'quick_answer'),
        '$.requiredArtifacts', JSON_ARRAY(
            'resume_facts', 'jd_requirements', 'technical_findings',
            'project_findings', 'risks', 'evidence_ledger', 'final_report'),
        '$.optionalArtifacts', JSON_ARRAY(),
        '$.jobFocus', 'ai_agent'),
    `update_time` = NOW()
WHERE `policy_id` = 'agent_job';

-- Rewrite-only policy.
UPDATE `policy_bundle`
SET `config` = JSON_SET(
        `config`,
        '$.supportedRunTypes', JSON_ARRAY('project_rewrite', 'resume_optimize'),
        '$.requiredArtifacts', JSON_ARRAY('resume_facts', 'project_findings', 'rewrite'),
        '$.optionalArtifacts', JSON_ARRAY()),
    `update_time` = NOW()
WHERE `policy_id` = 'resume_rewrite';
