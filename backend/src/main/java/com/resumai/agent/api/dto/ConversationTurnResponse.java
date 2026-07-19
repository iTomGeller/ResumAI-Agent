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
        List<String> affectedNodes,
        /** 本次消息创建/合并到的 Run；无 Run 时为 null。 */
        String runId,
        String runStatus,
        Integer queuePosition,
        String queueMode,
        /** INTERRUPT 时被取消的 Run。 */
        String interruptedRunId
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
                null, null, null, null, null);
    }
}
