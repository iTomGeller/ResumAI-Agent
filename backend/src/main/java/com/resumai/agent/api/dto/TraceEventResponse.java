package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;

public record TraceEventResponse(
        String traceId,
        String spanId,
        String parentSpanId,
        String agentRole,
        String eventType,
        String title,
        String detail,
        String status,
        Long durationMs,
        Integer tokenCost,
        LocalDateTime timestamp,
        // DAG 结构字段
        String dagGroupId,
        String laneId,
        String stepKind,
        String viewType,
        // HR 视图字段
        String businessLabel,
        String evidenceSummary,
        List<String> interviewHints,
        // 开发者视图字段
        String developerLabel,
        String skillName,
        String promptPreview,
        String inputSummary,
        String outputSummary,
        List<String> toolCalls,
        List<String> mcpCalls,
        String sandboxSummary
) {
    /** 向下兼容的旧构造方式 */
    public TraceEventResponse(String traceId, String spanId, String parentSpanId, String agentRole,
                              String eventType, String title, String detail, String status,
                              Long durationMs, Integer tokenCost, LocalDateTime timestamp) {
        this(traceId, spanId, parentSpanId, agentRole, eventType, title, detail, status,
                durationMs, tokenCost, timestamp,
                null, null, null, "BOTH", null, null, null, null, null, null, null, null, null, null, null);
    }
}
