package com.resumai.agent.ai;

import dev.langchain4j.model.chat.listener.*;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Listens to every ChatModel request/response and creates OTel spans -> Langfuse generations.
 * Captures multi-turn tool calling rounds so each LLM invocation is individually traced.
 */
@Component
public class TracingChatModelListener implements ChatModelListener {

    private static final Logger log = LoggerFactory.getLogger(TracingChatModelListener.class);
    private final Tracer tracer;
    private final AtomicInteger roundCounter = new AtomicInteger(0);
    private final Map<Object, Span> activeSpans = new ConcurrentHashMap<>();

    public TracingChatModelListener(OpenTelemetry openTelemetry) {
        this.tracer = openTelemetry.getTracer("resumai-chat-model");
    }

    @Override
    public void onRequest(ChatModelRequestContext context) {
        int round = roundCounter.incrementAndGet();

        long messageCount = context.chatRequest().messages() != null
                ? (long) context.chatRequest().messages().size() : 0L;

        Span span = tracer.spanBuilder("llm.chat.round-" + round)
                .setParent(Context.current())
                .setAttribute("langfuse.observation.type", "generation")
                .setAttribute("langfuse.observation.name", "DeepSeek Chat Round " + round)
                .setAttribute("gen_ai.system", "deepseek")
                .setAttribute("gen_ai.request.model", "deepseek-chat")
                .setAttribute("gen_ai.request.message_count", messageCount)
                .setAttribute("llm.round", (long) round)
                .startSpan();

        activeSpans.put(context, span);
        log.debug("LLM request round={} messages={}", round, messageCount);
    }

    @Override
    public void onResponse(ChatModelResponseContext context) {
        Span span = activeSpans.remove(context);
        if (span == null) {
            for (var entry : activeSpans.entrySet()) {
                span = entry.getValue();
                activeSpans.remove(entry.getKey());
                break;
            }
        }
        if (span == null) return;

        var response = context.chatResponse();
        String content = "";
        boolean hasToolCalls = false;

        if (response.aiMessage() != null) {
            if (response.aiMessage().text() != null) {
                content = response.aiMessage().text();
                if (content.length() > 1000) content = content.substring(0, 1000) + "...";
            }
            hasToolCalls = response.aiMessage().hasToolExecutionRequests();
        }

        long totalTokens = 0L;
        long inputTokens = 0L;
        long outputTokens = 0L;
        if (response.tokenUsage() != null) {
            totalTokens = response.tokenUsage().totalTokenCount() != null
                    ? (long) response.tokenUsage().totalTokenCount() : 0L;
            inputTokens = response.tokenUsage().inputTokenCount() != null
                    ? (long) response.tokenUsage().inputTokenCount() : 0L;
            outputTokens = response.tokenUsage().outputTokenCount() != null
                    ? (long) response.tokenUsage().outputTokenCount() : 0L;
        }

        span.setAttribute("langfuse.observation.output", content);
        span.setAttribute("llm.has_tool_calls", hasToolCalls);
        span.setAttribute("gen_ai.usage.input_tokens", inputTokens);
        span.setAttribute("gen_ai.usage.output_tokens", outputTokens);
        span.setAttribute("gen_ai.usage.total_tokens", totalTokens);

        if (hasToolCalls) {
            long toolCallCount = (long) response.aiMessage().toolExecutionRequests().size();
            span.setAttribute("llm.tool_call_count", toolCallCount);
            StringBuilder toolNames = new StringBuilder();
            for (var req : response.aiMessage().toolExecutionRequests()) {
                if (!toolNames.isEmpty()) toolNames.append(",");
                toolNames.append(req.name());
            }
            span.setAttribute("llm.tool_call_names", toolNames.toString());
        }

        span.setStatus(StatusCode.OK);
        span.end();

        log.debug("LLM response tokens={} toolCalls={}", totalTokens, hasToolCalls);
    }

    @Override
    public void onError(ChatModelErrorContext context) {
        Span span = activeSpans.remove(context);
        if (span == null) {
            for (var entry : activeSpans.entrySet()) {
                span = entry.getValue();
                activeSpans.remove(entry.getKey());
                break;
            }
        }
        if (span == null) return;

        span.setStatus(StatusCode.ERROR, context.error().getMessage());
        span.recordException(context.error());
        span.end();

        log.warn("LLM error: {}", context.error().getMessage());
    }
}
