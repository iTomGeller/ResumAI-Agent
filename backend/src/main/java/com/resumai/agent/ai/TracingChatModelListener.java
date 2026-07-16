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

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Component
public class TracingChatModelListener implements ChatModelListener {

    private static final Logger log = LoggerFactory.getLogger(TracingChatModelListener.class);
    private final Tracer tracer;
    private final AgentTraceCapture traceCapture;
    private final AtomicInteger roundCounter = new AtomicInteger(0);
    private final Map<Object, Span> activeSpans = new ConcurrentHashMap<>();
    private final Map<Object, String> requestInputs = new ConcurrentHashMap<>();

    public TracingChatModelListener(OpenTelemetry openTelemetry, AgentTraceCapture traceCapture) {
        this.tracer = openTelemetry.getTracer("resumai-chat-model");
        this.traceCapture = traceCapture;
    }

    @Override
    public void onRequest(ChatModelRequestContext context) {
        int round = roundCounter.incrementAndGet();

        var messages = context.chatRequest().messages();
        long messageCount = messages != null ? (long) messages.size() : 0L;

        AgentExecutionContext.Context ctx = AgentExecutionContext.get();
        if (ctx != null && messages != null) {
            List<AgentTraceCapture.ToolResultPair> toolResults = extractToolResults(messages);
            if (!toolResults.isEmpty()) {
                traceCapture.backfillToolResults(ctx.traceId(), ctx.agentName(), toolResults);
            }
        }

        String inputSummary = buildInputSummary(messages);
        requestInputs.put(context, inputSummary);

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
    }

    @Override
    public void onResponse(ChatModelResponseContext context) {
        Span span = activeSpans.remove(context);
        String inputSummary = requestInputs.remove(context);
        if (span == null) {
            for (var entry : activeSpans.entrySet()) {
                span = entry.getValue();
                activeSpans.remove(entry.getKey());
                break;
            }
        }
        if (inputSummary == null) {
            for (var entry : requestInputs.entrySet()) {
                inputSummary = entry.getValue();
                requestInputs.remove(entry.getKey());
                break;
            }
        }
        if (span == null) return;

        var response = context.chatResponse();
        String content = "";
        boolean hasToolCalls = false;
        List<AgentTraceCapture.ToolCallRecord> toolRecords = new ArrayList<>();

        if (response.aiMessage() != null) {
            if (response.aiMessage().text() != null) {
                content = response.aiMessage().text();
            }
            hasToolCalls = response.aiMessage().hasToolExecutionRequests();
            if (hasToolCalls && response.aiMessage().toolExecutionRequests() != null) {
                for (var req : response.aiMessage().toolExecutionRequests()) {
                    String toolType = classifyTool(req.name());
                    toolRecords.add(new AgentTraceCapture.ToolCallRecord(
                            req.name(), toolType,
                            req.arguments() != null ? truncate(req.arguments(), 1000) : "",
                            "", 0
                    ));
                }
            }
        }

        int totalTokens = 0;
        long inputTokens = 0L;
        long outputTokens = 0L;
        if (response.tokenUsage() != null) {
            totalTokens = response.tokenUsage().totalTokenCount() != null
                    ? response.tokenUsage().totalTokenCount() : 0;
            inputTokens = response.tokenUsage().inputTokenCount() != null
                    ? (long) response.tokenUsage().inputTokenCount() : 0L;
            outputTokens = response.tokenUsage().outputTokenCount() != null
                    ? (long) response.tokenUsage().outputTokenCount() : 0L;
        }

        String outputTrimmed = content.length() > 2000 ? content.substring(0, 2000) + "..." : content;
        span.setAttribute("langfuse.observation.output", outputTrimmed);
        span.setAttribute("llm.has_tool_calls", hasToolCalls);
        span.setAttribute("gen_ai.usage.input_tokens", inputTokens);
        span.setAttribute("gen_ai.usage.output_tokens", outputTokens);
        span.setAttribute("gen_ai.usage.total_tokens", (long) totalTokens);

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

        AgentExecutionContext.Context ctx = AgentExecutionContext.get();
        if (ctx != null) {
            int agentRound = getAgentRoundNumber(ctx.traceId(), ctx.agentName());
            traceCapture.recordLlmRound(
                    ctx.traceId(), ctx.agentName(), agentRound,
                    inputSummary != null ? inputSummary : "",
                    outputTrimmed,
                    totalTokens,
                    toolRecords
            );
        }

        log.debug("LLM response tokens={} toolCalls={} agent={}",
                totalTokens, hasToolCalls, ctx != null ? ctx.agentName() : "unknown");
    }

    @Override
    public void onError(ChatModelErrorContext context) {
        Span span = activeSpans.remove(context);
        requestInputs.remove(context);
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

    private final Map<String, AtomicInteger> agentRoundCounters = new ConcurrentHashMap<>();

    private int getAgentRoundNumber(String traceId, String agentName) {
        String key = traceId + ":" + agentName;
        return agentRoundCounters.computeIfAbsent(key, k -> new AtomicInteger(0)).incrementAndGet();
    }

    // --- Input Summary ---

    private String buildInputSummary(List<?> messages) {
        if (messages == null || messages.isEmpty()) return "";

        String systemPrompt = null;
        String userMessage = null;
        List<AgentTraceCapture.ToolResultPair> toolResults = new ArrayList<>();
        String lastAiMessage = null;

        for (Object msg : messages) {
            String raw = msg.toString();
            if (raw.contains("SystemMessage")) {
                systemPrompt = extractMessageContent(msg);
            } else if (raw.contains("UserMessage")) {
                userMessage = extractMessageContent(msg);
            } else if (raw.contains("ToolExecutionResult")) {
                String toolName = extractToolName(raw);
                String toolResult = extractMessageContent(msg);
                toolResults.add(new AgentTraceCapture.ToolResultPair(toolName, toolResult));
            } else if (raw.contains("AiMessage")) {
                lastAiMessage = extractMessageContent(msg);
            }
        }

        boolean isFollowUp = !toolResults.isEmpty();

        StringBuilder sb = new StringBuilder();
        if (isFollowUp) {
            if (lastAiMessage != null) {
                sb.append("[上轮 LLM 输出]: ");
                sb.append(truncate(lastAiMessage, 200));
                sb.append("\n");
            }
            for (AgentTraceCapture.ToolResultPair tr : toolResults) {
                sb.append("[Tool: ").append(tr.name()).append("] → ");
                sb.append(truncate(tr.result(), 300));
                sb.append("\n");
            }
        } else {
            if (systemPrompt != null) {
                sb.append(truncate(systemPrompt, 800));
            }
            if (userMessage != null) {
                if (!sb.isEmpty()) sb.append("\n---\n[用户输入]\n");
                sb.append(truncate(userMessage, 800));
            }
        }

        return sb.toString();
    }

    // --- Tool Result Extraction ---

    private List<AgentTraceCapture.ToolResultPair> extractToolResults(List<?> messages) {
        List<AgentTraceCapture.ToolResultPair> results = new ArrayList<>();
        for (Object msg : messages) {
            String raw = msg.toString();
            if (raw.contains("ToolExecutionResult")) {
                String toolName = extractToolName(raw);
                String toolResult = extractMessageContent(msg);
                results.add(new AgentTraceCapture.ToolResultPair(toolName, toolResult));
            }
        }
        return results;
    }

    private static final Pattern TOOL_NAME_PATTERN = Pattern.compile("toolName\\s*=\\s*\"?([\\w_]+)\"?");
    private static final Pattern TOOL_NAME_PATTERN2 = Pattern.compile("name\\s*=\\s*\"?([\\w_]+)\"?");

    private String extractToolName(String raw) {
        Matcher m = TOOL_NAME_PATTERN.matcher(raw);
        if (m.find()) return m.group(1);
        Matcher m2 = TOOL_NAME_PATTERN2.matcher(raw);
        if (m2.find()) return m2.group(1);
        return "unknown_tool";
    }

    private String extractMessageContent(Object msg) {
        if (msg == null) return "";
        String raw = msg.toString();
        int textStart = raw.indexOf("text = \"");
        if (textStart >= 0) {
            int contentStart = textStart + 8;
            int contentEnd = raw.lastIndexOf("\"");
            if (contentEnd > contentStart) {
                return raw.substring(contentStart, contentEnd);
            }
        }
        int textStart2 = raw.indexOf("text=\"");
        if (textStart2 >= 0) {
            int contentStart = textStart2 + 6;
            int contentEnd = raw.lastIndexOf("\"");
            if (contentEnd > contentStart) {
                return raw.substring(contentStart, contentEnd);
            }
        }
        return raw.length() > 500 ? raw.substring(0, 500) + "..." : raw;
    }

    private String classifyTool(String toolName) {
        if (toolName == null) return "tool";
        if (toolName.startsWith("mcp_")) return "mcp";
        if (toolName.startsWith("execute_skill") || toolName.startsWith("list_skills")) return "skill";
        return "tool";
    }

    private String truncate(String s, int max) {
        if (s == null) return "";
        return s.length() <= max ? s : s.substring(0, max) + "...";
    }
}
