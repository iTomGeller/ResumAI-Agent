package com.resumai.agent.ai;

import dev.langchain4j.model.chat.listener.*;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
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
 * Listens to every ChatModel request/response and creates OTel spans → Langfuse generations.
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
        String modelName = "deepseek-chat";

        int messageCount = context.chatRequest().messages() != null
                ? context.chatRequest().messages().size() : 0;

        Span span = tracer.spanBuilder("llm.chat.round-" + round)
                .setParent(Context.current())
                .setAttribute("langfuse.observation.type", "generation")
                .setAttribute("langfuse.observation.name", "DeepSeek Chat Round " + round)
                .setAttribute(AttributeKey.stringKey("gen_ai.system"), "deepseek")
                .setAttribute(AttributeKey.stringKey("gen_ai.request.model"), modelName)
                .setAttribute(AttributeKey.longKey("gen_ai.request.message_count"), messageCount)
                .setAttribute(AttributeKey.longKey("llm.round"), round)
                .startSpan();

        activeSpans.put(context, span);
        log.debug("LLM request round={} messages={}", round, messageCount);
    }

    @Override
    public void onResponse(ChatModelResponseContext context) {
        Span span = activeSpans.remove(context.requestContext());
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

        int totalTokens = 0;
        int inputTokens = 0;
        int outputTokens = 0;
        if (response.tokenUsage() != null) {
            totalTokens = response.tokenUsage().totalTokenCount() != null
                    ? response.tokenUsage().totalTokenCount() : 0;
            inputTokens = response.tokenUsage().inputTokenCount() != null
                    ? response.tokenUsage().inputTokenCount() : 0;
            outputTokens = response.tokenUsage().outputTokenCount() != null
                    ? response.tokenUsage().outputTokenCount() : 0;
        }

        span.setAttribute("langfuse.observation.output", content);
        span.setAttribute(AttributeKey.booleanKey("llm.has_tool_calls"), hasToolCalls);
        span.setAttribute(AttributeKey.longKey("gen_ai.usage.input_tokens"), inputTokens);
        span.setAttribute(AttributeKey.longKey("gen_ai.usage.output_tokens"), outputTokens);
        span.setAttribute(AttributeKey.longKey("gen_ai.usage.total_tokens"), totalTokens);

        if (hasToolCalls) {
            int toolCallCount = response.aiMessage().toolExecutionRequests().size();
            span.setAttribute(AttributeKey.longKey("llm.tool_call_count"), toolCallCount);
            StringBuilder toolNames = new StringBuilder();
            for (var req : response.aiMessage().toolExecutionRequests()) {
                if (toolNames.length() > 0) toolNames.append(",");
                toolNames.append(req.name());
            }
            span.setAttribute(AttributeKey.stringKey("llm.tool_call_names"), toolNames.toString());
        }

        span.setStatus(StatusCode.OK);
        span.end();

        log.debug("LLM response tokens={} toolCalls={}", totalTokens, hasToolCalls);
    }

    @Override
    public void onError(ChatModelErrorContext context) {
        Span span = activeSpans.remove(context.requestContext());
        if (span == null) return;

        span.setStatus(StatusCode.ERROR, context.error().getMessage());
        span.recordException(context.error());
        span.end();

        log.warn("LLM error: {}", context.error().getMessage());
    }
}
