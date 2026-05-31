package com.resumai.agent.ai;

import com.resumai.agent.config.AgentMetrics;
import com.resumai.agent.config.DeepSeekProperties;
import com.resumai.agent.domain.entity.LlmInvocation;
import com.resumai.agent.service.LlmInvocationService;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.output.TokenUsage;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * DeepSeek Chat 客户端 -- 基于 LangChain4j ChatModel。
 */
@Component
public class DeepSeekClient {

    private static final String SYSTEM_PROMPT =
            "你是企业招聘场景的资深 AI 简历评估 Agent，请输出严谨、可执行、中文结构化结论。";
    private static final String MODEL_NAME = "deepseek-chat";
    private static final double INPUT_COST_PER_1K = 0.001;
    private static final double OUTPUT_COST_PER_1K = 0.002;

    private final ChatModel chatModel;
    private final DeepSeekProperties properties;
    private final AgentMetrics agentMetrics;
    private final LlmInvocationService llmInvocationService;

    public DeepSeekClient(ChatModel chatModel,
                          DeepSeekProperties properties,
                          AgentMetrics agentMetrics,
                          LlmInvocationService llmInvocationService) {
        this.chatModel = chatModel;
        this.properties = properties;
        this.agentMetrics = agentMetrics;
        this.llmInvocationService = llmInvocationService;
    }

    public String evaluateResume(String prompt) {
        return evaluateResume(prompt, "DeepSeekChatModel", "evaluation", null, null).text();
    }

    public String evaluateResume(String prompt, String agent, String purpose) {
        return evaluateResume(prompt, agent, purpose, null, null).text();
    }

    public LlmCallResult evaluateResume(String prompt, String agent, String purpose, String traceId, String spanId) {
        if (!StringUtils.hasText(properties.getApiKey())) {
            String fallback = "DeepSeek API Key 未配置，当前返回 MVP 本地评估：候选人具备基础岗位匹配度，建议补充真实简历文本后进行 AI 深度评估。";
            LlmInvocation saved = llmInvocationService.saveInvocation(
                    traceId, spanId, MODEL_NAME, agent, purpose, 0L,
                    prompt, fallback, estimateTokens(prompt), estimateTokens(fallback),
                    "fallback", null, null);
            return new LlmCallResult(fallback, saved.getId(), false,
                    prompt == null ? 0 : prompt.length(), fallback.length(),
                    estimateTokens(prompt), estimateTokens(fallback), "fallback");
        }

        long start = System.currentTimeMillis();
        try {
            ChatResponse response = chatModel.chat(
                    SystemMessage.from(SYSTEM_PROMPT),
                    UserMessage.from(prompt)
            );
            long durationMs = System.currentTimeMillis() - start;
            agentMetrics.recordLlmDuration(MODEL_NAME, agent, purpose, durationMs);

            TokenUsage tokenUsage = response.tokenUsage();
            int inputTokens = tokenUsage != null && tokenUsage.inputTokenCount() != null
                    ? tokenUsage.inputTokenCount() : estimateTokens(prompt);

            AiMessage aiMessage = response.aiMessage();
            String text = aiMessage.text();
            if (!StringUtils.hasText(text)) {
                throw new IllegalStateException("DeepSeek 返回内容为空");
            }

            int outputTokens = tokenUsage != null && tokenUsage.outputTokenCount() != null
                    ? tokenUsage.outputTokenCount() : estimateTokens(text);
            String finishReason = "stop";

            agentMetrics.recordLlmTokens(MODEL_NAME, agent, purpose, inputTokens, outputTokens);
            double costUsd = (inputTokens * INPUT_COST_PER_1K / 1000D) + (outputTokens * OUTPUT_COST_PER_1K / 1000D);
            agentMetrics.recordLlmCostPerCall(MODEL_NAME, agent, costUsd);
            agentMetrics.recordLlmContextUtilization(MODEL_NAME, Math.min(1D, inputTokens / 64000D));

            LlmInvocation saved = llmInvocationService.saveInvocation(
                    traceId, spanId, MODEL_NAME, agent, purpose, durationMs,
                    prompt, text, inputTokens, outputTokens, finishReason, null, null);
            boolean truncated = finishReason.toLowerCase().contains("length");
            if (truncated) {
                agentMetrics.recordLlmError(MODEL_NAME, "TruncatedResponse");
            }
            return new LlmCallResult(text, saved.getId(), truncated,
                    prompt.length(), text.length(), inputTokens, outputTokens, finishReason);
        } catch (Exception e) {
            agentMetrics.recordLlmDuration(MODEL_NAME, agent, purpose, System.currentTimeMillis() - start);
            agentMetrics.recordLlmError(MODEL_NAME, e.getClass().getSimpleName());
            LlmInvocation saved = llmInvocationService.saveInvocation(
                    traceId, spanId, MODEL_NAME, agent, purpose, System.currentTimeMillis() - start,
                    prompt, null, estimateTokens(prompt), 0, "error",
                    e.getClass().getSimpleName(), e.getMessage());
            throw e instanceof RuntimeException runtime ? runtime : new IllegalStateException(e.getMessage(), e);
        }
    }

    private static int estimateTokens(String text) {
        if (!StringUtils.hasText(text)) {
            return 0;
        }
        return Math.max(1, text.length() / 4);
    }
}
