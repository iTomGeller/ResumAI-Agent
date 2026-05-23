package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

/**
 * Agent 执行 Trace 事件响应。
 *
 * <p>每条事件对应一次 Agent、Skill、MCP、RAG、LLM 或 RAGAS 的关键动作，
 * 前端通过该结构渲染实时瀑布流和 Span 明细。</p>
 */
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
        LocalDateTime timestamp
) {
}
