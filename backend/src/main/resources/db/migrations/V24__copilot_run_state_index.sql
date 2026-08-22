-- One covering access path for Copilot's active/paused/pending run snapshot.
-- @guard index:agent_run.idx_agent_run_conv_status_created
ALTER TABLE `agent_run`
    ADD INDEX `idx_agent_run_conv_status_created`
        (`conversation_id`, `status`, `created_at`);
