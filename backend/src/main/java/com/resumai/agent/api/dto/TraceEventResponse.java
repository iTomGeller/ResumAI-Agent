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
        String sandboxSummary,
        String llmInvocationId,
        // DAG 契约扩展
        String nodeId,
        List<String> dependsOn,
        String edgeLabel,
        String phase,
        Boolean expected,
        Integer sortOrder,
        // 完整可追溯详情（摘要仍保留在 preview/summary 字段）
        String fullPrompt,
        String fullInput,
        String fullOutput,
        // 轮次 / 调用粒度扩展
        Integer sequence,
        Integer roundIndex,
        String roundRole,
        String callKind,
        String callName,
        String parentAgentSpanId,
        String parentRoundId,
        String ioJson
) {
    /** 向下兼容的旧构造方式 */
    public TraceEventResponse(String traceId, String spanId, String parentSpanId, String agentRole,
                              String eventType, String title, String detail, String status,
                              Long durationMs, Integer tokenCost, LocalDateTime timestamp) {
        this(traceId, spanId, parentSpanId, agentRole, eventType, title, detail, status,
                durationMs, tokenCost, timestamp,
                null, null, null, "BOTH",
                null, null, null,
                null, null, null, null, null,
                null, null, null, null,
                null, null, null, null, null, null,
                null, null, null,
                null, null, null, null, null, null, null, null);
    }
}
