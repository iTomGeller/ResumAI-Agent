package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

public record LlmInvocationResponse(
        String id,
        String traceId,
        String spanId,
        String model,
        String agentRole,
        String purpose,
        LocalDateTime requestStartedAt,
        Long durationMs,
        Integer inputTokens,
        Integer outputTokens,
        String finishReason,
        Boolean truncated,
        Integer promptChars,
        Integer responseChars,
        String promptPreview,
        String responsePreview,
        String promptFull,
        String responseFull,
        String errorCode
) {
}
