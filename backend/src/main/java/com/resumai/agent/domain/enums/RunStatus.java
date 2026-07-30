package com.resumai.agent.domain.enums;

import java.util.Set;

/** Conversational agent run lifecycle. */
public enum RunStatus {
    QUEUED,
    STARTING,
    RUNNING,
    WAITING_LLM,
    WAITING_TOOL,
    PAUSING,
    PAUSED,
    RESUMING,
    CANCELLING,
    CANCELLED,
    SUCCEEDED,
    PARTIAL_SUCCESS,
    FAILED,
    TIMED_OUT;

    public static final Set<String> TERMINAL = Set.of(
            CANCELLED.name(), SUCCEEDED.name(), PARTIAL_SUCCESS.name(),
            FAILED.name(), TIMED_OUT.name());

    /** States holding permits and expected to make progress (watchdog scope). */
    public static final Set<String> ACTIVE = Set.of(
            STARTING.name(), RUNNING.name(), WAITING_LLM.name(),
            WAITING_TOOL.name(),
            PAUSING.name(), RESUMING.name(), CANCELLING.name());

    public static boolean isTerminal(String status) {
        return status != null && TERMINAL.contains(status);
    }

    public static boolean isActive(String status) {
        return status != null && ACTIVE.contains(status);
    }

    public static boolean isPaused(String status) {
        return PAUSED.name().equals(status);
    }
}
