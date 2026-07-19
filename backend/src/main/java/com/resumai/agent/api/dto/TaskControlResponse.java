package com.resumai.agent.api.dto;

public record TaskControlResponse(
        String traceId,
        String workflowRunId,
        String action,
        String status,
        String message
) {
}
