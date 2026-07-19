package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;

public record ConversationSnapshotResponse(
        String conversationId,
        String activeTraceId,
        Integer activeRevision,
        List<Message> messages,
        List<Revision> revisions
) {
    public record Message(
            Long id,
            String clientMessageId,
            String role,
            String intent,
            String content,
            Integer revision,
            LocalDateTime createdAt
    ) {
    }

    public record Revision(
            String traceId,
            Integer revision,
            String status,
            String workflowRunId,
            String supersedesTraceId,
            String supersededByTraceId,
            String evaluationBrief,
            LocalDateTime createdAt
    ) {
    }
}
