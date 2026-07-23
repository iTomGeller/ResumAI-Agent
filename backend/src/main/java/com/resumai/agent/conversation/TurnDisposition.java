package com.resumai.agent.conversation;

/**
 * Server-owned decision for one conversation turn. Ordinary chat never becomes
 * an evaluation AgentRun; only revision / supersede dispositions enqueue work.
 */
public enum TurnDisposition {
    /** Short Copilot answer — no evaluation run, no ReportAgent. */
    DIRECT_REPLY,
    /** Lightweight evidence / retrieval turn; still CopilotAnswer, not StructuredReport. */
    BACKGROUND_QUERY,
    /** Fold a candidate fact into an unconsumed pending run. */
    MERGE_CONTEXT,
    /** Create a minimal evaluation revision. */
    CREATE_REVISION,
    /** Cancel / replace the active evaluation run at a safe boundary. */
    SUPERSEDE_RUN,
    /** Explicit stop / pause / resume. */
    CONTROL
}
