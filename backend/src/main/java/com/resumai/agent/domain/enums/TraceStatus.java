package com.resumai.agent.domain.enums;

/**
 * Agent Span 执行状态。
 */
public enum TraceStatus {

    /**
     * Span 已开始执行。
     */
    STARTED,

    /**
     * Span 正常执行完成。
     */
    SUCCESS,

    /**
     * Span 执行失败。
     */
    FAILED,

    /**
     * Span 触发重试。
     */
    RETRYING,

    /**
     * Span 被降级处理。
     */
    DEGRADED,

    /**
     * Span 因超时被终止。
     */
    TIMEOUT
}
