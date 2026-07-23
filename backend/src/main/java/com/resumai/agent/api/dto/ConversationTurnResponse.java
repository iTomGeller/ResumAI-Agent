package com.resumai.agent.api.dto;

import java.util.List;
import java.util.Map;

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
        List<String> affectedNodes,
        String runId,
        String runStatus,
        Integer queuePosition,
        String queueMode,
        String interruptedRunId,
        String disposition,
        String reason,
        String turnId,
        List<Map<String, Object>> citations,
        List<Map<String, Object>> actions,
        List<String> suggestions
) {
    public static ConversationTurnResponse legacy(String conversationId, String clientMessageId,
                                                  String intent, boolean affectsEvaluation,
                                                  boolean answerThenResume, boolean needsConfirmation,
                                                  String action, String assistantMessage,
                                                  String activeTraceId, Integer activeRevision,
                                                  String supersededTraceId, List<String> affectedNodes) {
        return new ConversationTurnResponse(conversationId, clientMessageId, intent,
                affectsEvaluation, answerThenResume, needsConfirmation, action, assistantMessage,
                activeTraceId, activeRevision, supersededTraceId, affectedNodes,
                null, null, null, null, null,
                null, null, null, List.of(), List.of(), List.of());
    }

    public ConversationTurnResponse withDisposition(String disposition, String reason, String turnId) {
        return new ConversationTurnResponse(
                conversationId, clientMessageId, intent, affectsEvaluation, answerThenResume,
                needsConfirmation, action, assistantMessage, activeTraceId, activeRevision,
                supersededTraceId, affectedNodes, runId, runStatus, queuePosition, queueMode,
                interruptedRunId, disposition, reason, turnId, citations, actions, suggestions);
    }
}
