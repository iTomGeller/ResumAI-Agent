package com.resumai.agent.api.dto;

import java.util.List;

public record ConversationTurnResponse(
        String conversationId,
        String clientMessageId,
        String intent,
        boolean affectsEvaluation,
        boolean answerThenResume,
        boolean needsConfirmation,
        String action,
        String assistantMessage,
        String activeTraceId,
        Integer activeRevision,
        String supersededTraceId,
        List<String> affectedNodes
) {
}
