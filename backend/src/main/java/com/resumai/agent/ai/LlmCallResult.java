package com.resumai.agent.ai;

/**
 * DeepSeek 调用结果，包含完整调用 ID 与规模信息。
 */
public record LlmCallResult(
        String text,
        String llmInvocationId,
        boolean truncated,
        int promptChars,
        int responseChars,
        int inputTokens,
        int outputTokens,
        String finishReason
) {
}
