package com.resumai.agent.conversation;

import java.util.List;

public record TurnDecision(
        TurnDisposition disposition,
        String intent,
        List<String> invalidatedArtifacts,
        String controlAction,
        double confidence,
        String reason,
        boolean needsConfirmation
) {
    public TurnDecision(TurnDisposition disposition, String intent, String reason) {
        this(disposition, intent, List.of(), null, 1.0, reason, false);
    }

    public TurnDecision withControl(String action) {
        return new TurnDecision(disposition, intent, invalidatedArtifacts, action,
                confidence, reason, needsConfirmation);
    }
}
