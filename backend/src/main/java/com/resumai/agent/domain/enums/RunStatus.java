package com.resumai.agent.domain.enums;

import java.util.Set;

/** Conversational agent run lifecycle. */
public enum RunStatus {
    QUEUED,
    STARTING,
    RUNNING,
    WAITING_LLM,
    WAITING_TOOL,
    WAITING_SANDBOX,
    CANCELLING,
    CANCELLED,
    SUCCEEDED,
    FAILED,
    TIMED_OUT;

    public static final Set<String> TERMINAL = Set.of(
            CANCELLED.name(), SUCCEEDED.name(), FAILED.name(), TIMED_OUT.name());

    public static final Set<String> ACTIVE = Set.of(
            STARTING.name(), RUNNING.name(), WAITING_LLM.name(),
            WAITING_TOOL.name(), WAITING_SANDBOX.name(), CANCELLING.name());

    public static boolean isTerminal(String status) {
        return status != null && TERMINAL.contains(status);
    }

    public static boolean isActive(String status) {
        return status != null && ACTIVE.contains(status);
    }
}
