package com.resumai.agent.ai;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.AgentMetrics;
import com.resumai.agent.config.DeepSeekProperties;
import com.resumai.agent.domain.entity.LlmInvocation;
import com.resumai.agent.service.LlmInvocationService;
import dev.langchain4j.data.message.*;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.chat.request.ChatRequest;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.output.TokenUsage;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * DeepSeek Chat 客户端 -- 基于 LangChain4j ChatModel。
 * 支持单次调用和多轮 Agent Loop（含 tool calling）。
 */
@Component
public class DeepSeekClient {

    private static final Logger log = LoggerFactory.getLogger(DeepSeekClient.class);
    private static final ObjectMapper JSON_MAPPER = new ObjectMapper();

    private static final String SYSTEM_PROMPT =
            "你是企业招聘场景的资深 AI 简历评估 Agent，请输出严谨、可执行、中文结构化结论。";
    private static final String MODEL_NAME = "deepseek-chat";
    private static final double INPUT_COST_PER_1K = 0.001;
    private static final double OUTPUT_COST_PER_1K = 0.002;

    private final ChatModel chatModel;
    private final DeepSeekProperties properties;
    private final AgentMetrics agentMetrics;
    private final LlmInvocationService llmInvocationService;
    private final Tracer tracer;

    public DeepSeekClient(ChatModel chatModel,
                          DeepSeekProperties properties,
                          AgentMetrics agentMetrics,
                          LlmInvocationService llmInvocationService,
                          OpenTelemetry openTelemetry) {
        this.chatModel = chatModel;
        this.properties = properties;
        this.agentMetrics = agentMetrics;
        this.llmInvocationService = llmInvocationService;
        this.tracer = openTelemetry.getTracer("resumai-agent");
    }

    // ===== Legacy single-call methods (unchanged) =====

    public String evaluateResume(String prompt) {
        return evaluateResume(prompt, "DeepSeekChatModel", "evaluation", null, null).text();
    }

    public String evaluateResume(String prompt, String agent, String purpose) {
        return evaluateResume(prompt, agent, purpose, null, null).text();
    }

    public LlmCallResult evaluateResume(String prompt, String agent, String purpose, String traceId, String spanId) {
        if (!StringUtils.hasText(properties.getApiKey())) {
            String error = "DeepSeek API Key 未配置；拒绝生成无模型依据的候选人评估。";
            llmInvocationService.saveInvocation(
                    traceId, spanId, MODEL_NAME, agent, purpose, 0L,
                    prompt, null, estimateTokens(prompt), 0,
                    "error", "MODEL_CREDENTIAL_MISSING", error);
            throw new IllegalStateException(error);
        }

        Span llmSpan = tracer.spanBuilder("deepseek-chat")
                .setAttribute("langfuse.observation.type", "generation")
                .setAttribute("gen_ai.system", "deepseek")
                .setAttribute("gen_ai.request.model", MODEL_NAME)
                .setAttribute("langfuse.observation.input", prompt != null ? prompt : "")
                .setAttribute("agent", agent)
                .setAttribute("purpose", purpose)
                .startSpan();

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

            llmSpan.setAttribute("langfuse.observation.output", text);
            llmSpan.setAttribute("gen_ai.usage.input_tokens", (long) inputTokens);
            llmSpan.setAttribute("gen_ai.usage.output_tokens", (long) outputTokens);
            llmSpan.setStatus(StatusCode.OK);
            llmSpan.end();

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
            llmSpan.setStatus(StatusCode.ERROR, e.getMessage());
            llmSpan.recordException(e);
            llmSpan.end();
            LlmInvocation saved = llmInvocationService.saveInvocation(
                    traceId, spanId, MODEL_NAME, agent, purpose, System.currentTimeMillis() - start,
                    prompt, null, estimateTokens(prompt), 0, "error",
                    e.getClass().getSimpleName(), e.getMessage());
            throw e instanceof RuntimeException runtime ? runtime : new IllegalStateException(e.getMessage(), e);
        }
    }

    // ===== Helpers =====

    public Tracer getTracer() {
        return tracer;
    }

    private static int estimateTokens(String text) {
        if (!StringUtils.hasText(text)) return 0;
        return Math.max(1, text.length() / 4);
    }

    private static int estimateTokens(List<ChatMessage> messages) {
        return messages.stream()
                .mapToInt(m -> {
                    if (m instanceof SystemMessage sm) return estimateTokens(sm.text());
                    if (m instanceof UserMessage um) return estimateTokens(um.singleText());
                    if (m instanceof AiMessage am) return estimateTokens(am.text());
                    if (m instanceof ToolExecutionResultMessage tm) return estimateTokens(tm.text());
                    return 10;
                })
                .sum();
    }

    private List<Map<String, String>> summarizeMessages(List<ChatMessage> messages) {
        return messages.stream().map(m -> {
            Map<String, String> entry = new LinkedHashMap<>();
            if (m instanceof SystemMessage sm) {
                entry.put("role", "system");
                entry.put("content", trim(sm.text(), 200));
            } else if (m instanceof UserMessage um) {
                entry.put("role", "user");
                entry.put("content", trim(um.singleText(), 500));
            } else if (m instanceof AiMessage am) {
                entry.put("role", "assistant");
                if (am.hasToolExecutionRequests()) {
                    entry.put("tool_calls", am.toolExecutionRequests().stream()
                            .map(r -> r.name() + "(" + trim(r.arguments(), 100) + ")")
                            .toList().toString());
                } else {
                    entry.put("content", trim(am.text(), 300));
                }
            } else if (m instanceof ToolExecutionResultMessage tm) {
                entry.put("role", "tool");
                entry.put("content", trim(tm.text(), 200));
            }
            return entry;
        }).toList();
    }

    private static String trim(String text, int maxLen) {
        if (text == null) return "";
        return text.length() <= maxLen ? text : text.substring(0, maxLen) + "...";
    }

    private static String toJson(Object obj) {
        try {
            return JSON_MAPPER.writeValueAsString(obj);
        } catch (JsonProcessingException e) {
            return obj.toString();
        }
    }
}
