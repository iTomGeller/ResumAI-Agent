package com.resumai.agent.service.run;

/**
 * Raised when a policy selection cannot be persisted (e.g. column truncation).
 * Distinct from generic runtime start failures so the UI can show the correct
 * control-plane stage.
 */
public class PolicySelectionPersistenceException extends RuntimeException {

    private final String runId;
    private final String selectionMode;

    public PolicySelectionPersistenceException(String runId, String selectionMode, Throwable cause) {
        super("policy selection persist failed run=" + runId
                + " mode=" + selectionMode + ": " + (cause != null ? cause.getMessage() : ""),
                cause);
        this.runId = runId;
        this.selectionMode = selectionMode;
    }

    public String runId() {
        return runId;
    }

    public String selectionMode() {
        return selectionMode;
    }
}
