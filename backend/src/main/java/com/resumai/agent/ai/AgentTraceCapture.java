package com.resumai.agent.ai;

import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class AgentTraceCapture {

    private static final Logger log = LoggerFactory.getLogger(AgentTraceCapture.class);
    private final Tracer tracer;

    private final Map<String, TraceSession> activeSessions = new ConcurrentHashMap<>();
    private final Map<String, TraceSession> completedSessions = new ConcurrentHashMap<>();

    @FunctionalInterface
    public interface AgentPersistenceListener {
        void onAgentCompleted(String traceId, AgentEvent event);
    }

    private volatile AgentPersistenceListener persistenceListener;

    public void setPersistenceListener(AgentPersistenceListener listener) {
        this.persistenceListener = listener;
    }

    public AgentTraceCapture(OpenTelemetry openTelemetry) {
        this.tracer = openTelemetry.getTracer("resumai-agent-orchestrator");
    }

    public void begin(String traceId) {
        TraceSession session = new TraceSession(traceId);
        activeSessions.put(traceId, session);
        log.debug("Trace session started: {}", traceId);
    }

    public String getActiveTraceId() {
        for (Map.Entry<String, TraceSession> entry : activeSessions.entrySet()) {
            if ("RUNNING".equals(entry.getValue().status) || entry.getValue().status == null) {
                return entry.getKey();
            }
        }
        return activeSessions.keySet().stream().findFirst().orElse(null);
    }

    public void agentStart(String agentName, String description, int phase) {
        String threadTraceId = getActiveTraceId();
        if (threadTraceId == null) return;

        TraceSession session = activeSessions.get(threadTraceId);
        if (session == null) return;

        Span agentSpan = tracer.spanBuilder(agentName)
                .setParent(Context.current())
                .setAttribute("langfuse.observation.type", "span")
                .setAttribute("langfuse.observation.name", agentName)
                .setAttribute(AttributeKey.stringKey("agent.name"), agentName)
                .setAttribute(AttributeKey.stringKey("agent.description"), description)
                .setAttribute(AttributeKey.longKey("agent.phase"), (long) phase)
                .startSpan();

        session.pushSpan(agentName, agentSpan);

        AgentEvent event = new AgentEvent();
        event.agentName = agentName;
        event.description = description;
        event.phase = phase;
        event.startTimeMs = System.currentTimeMillis();
        event.status = "RUNNING";
        session.events.add(event);
    }

    public void recordLlmRound(String traceId, String agentName, int roundNum,
                                String input, String output, int totalTokens,
                                List<ToolCallRecord> toolCalls) {
        TraceSession session = activeSessions.get(traceId);
        if (session == null) return;

        for (AgentEvent event : session.events) {
            if (event.agentName.equals(agentName) && "RUNNING".equals(event.status)) {
                LlmRound round = new LlmRound();
                round.roundNum = roundNum;
                round.input = input;
                round.output = output;
                round.tokens = totalTokens;
                round.toolCalls = toolCalls != null ? toolCalls : List.of();
                event.rounds.add(round);
                break;
            }
        }
    }

    public void backfillToolResults(String traceId, String agentName,
                                     List<ToolResultPair> toolResults) {
        TraceSession session = activeSessions.get(traceId);
        if (session == null) return;

        for (AgentEvent event : session.events) {
            if (event.agentName.equals(agentName) && "RUNNING".equals(event.status)) {
                if (event.rounds.isEmpty()) break;
                LlmRound lastRound = event.rounds.get(event.rounds.size() - 1);
                if (lastRound.toolCalls.isEmpty()) break;

                int resultIdx = 0;
                for (ToolCallRecord tc : lastRound.toolCalls) {
                    if (resultIdx < toolResults.size()) {
                        ToolResultPair entry = toolResults.get(resultIdx);
                        tc.result = entry.result() != null ? truncateResult(entry.result(), 1000) : "";
                        resultIdx++;
                    }
                }
                for (; resultIdx < toolResults.size(); resultIdx++) {
                    ToolResultPair entry = toolResults.get(resultIdx);
                    lastRound.toolCalls.add(new ToolCallRecord(
                            entry.name(), classifyTool(entry.name()),
                            "", entry.result() != null ? truncateResult(entry.result(), 1000) : "", 0));
                }
                break;
            }
        }
    }

    private String truncateResult(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max) + "...";
    }

    private String classifyTool(String name) {
        if (name == null) return "tool";
        if (name.startsWith("mcp_")) return "mcp";
        if (name.startsWith("execute_skill") || name.startsWith("list_skills")) return "skill";
        return "tool";
    }

    public void agentEnd(String agentName, String status, long durationMs, String output) {
        String threadTraceId = getActiveTraceId();
        if (threadTraceId == null) return;

        TraceSession session = activeSessions.get(threadTraceId);
        if (session == null) return;

        Span span = session.popSpan(agentName);
        if (span != null) {
            String outputTrimmed = output != null && output.length() > 2000 ? output.substring(0, 2000) : output;
            span.setAttribute("langfuse.observation.output", outputTrimmed != null ? outputTrimmed : "");
            span.setAttribute(AttributeKey.longKey("agent.durationMs"), durationMs);
            span.setAttribute(AttributeKey.stringKey("agent.status"), status);
            if ("FAILED".equals(status)) {
                span.setStatus(StatusCode.ERROR, output);
            } else {
                span.setStatus(StatusCode.OK);
            }
            span.end();
        }

        AgentEvent completedEvent = null;
        for (AgentEvent event : session.events) {
            if (event.agentName.equals(agentName) && "RUNNING".equals(event.status)) {
                event.status = status;
                event.durationMs = durationMs;
                event.output = output != null && output.length() > 2000 ? output.substring(0, 2000) : output;
                completedEvent = event;
                break;
            }
        }

        if (completedEvent != null && persistenceListener != null) {
            try {
                persistenceListener.onAgentCompleted(threadTraceId, completedEvent);
            } catch (Exception e) {
                log.warn("Persistence listener failed for agent {}: {}", agentName, e.getMessage());
            }
        }
    }

    public void end(String traceId, String status, long totalDurationMs) {
        TraceSession session = activeSessions.remove(traceId);
        if (session == null) return;
        session.totalDurationMs = totalDurationMs;
        session.status = status;
        completedSessions.put(traceId, session);
        log.debug("Trace session ended: {} status={} duration={}ms agents={}",
                traceId, status, totalDurationMs, session.events.size());
    }

    public List<AgentEvent> getEvents(String traceId) {
        TraceSession session = activeSessions.get(traceId);
        if (session == null) session = completedSessions.get(traceId);
        return session != null ? new ArrayList<>(session.events) : List.of();
    }

    public List<AgentEvent> consumeEvents(String traceId) {
        TraceSession session = completedSessions.remove(traceId);
        if (session == null) session = activeSessions.get(traceId);
        return session != null ? new ArrayList<>(session.events) : List.of();
    }

    // --- Data Model ---

    public static class TraceSession {
        final String traceId;
        final List<AgentEvent> events = Collections.synchronizedList(new ArrayList<>());
        final Map<String, Deque<Span>> spanStacks = new ConcurrentHashMap<>();
        String status;
        long totalDurationMs;

        TraceSession(String traceId) { this.traceId = traceId; }

        void pushSpan(String agentName, Span span) {
            spanStacks.computeIfAbsent(agentName, k -> new ArrayDeque<>()).push(span);
        }

        Span popSpan(String agentName) {
            Deque<Span> stack = spanStacks.get(agentName);
            return stack != null && !stack.isEmpty() ? stack.pop() : null;
        }
    }

    public static class AgentEvent {
        public String agentName;
        public String description;
        public int phase;
        public long startTimeMs;
        public long durationMs;
        public String status;
        public String output;
        public final List<LlmRound> rounds = Collections.synchronizedList(new ArrayList<>());
    }

    public static class LlmRound {
        public int roundNum;
        public String input;
        public String output;
        public int tokens;
        public List<ToolCallRecord> toolCalls = new ArrayList<>();
    }

    public static class ToolCallRecord {
        public String name;
        public String type;
        public String arguments;
        public String result;
        public long durationMs;

        public ToolCallRecord() {}

        public ToolCallRecord(String name, String type, String arguments, String result, long durationMs) {
            this.name = name;
            this.type = type;
            this.arguments = arguments;
            this.result = result;
            this.durationMs = durationMs;
        }
    }

    public record ToolResultPair(String name, String result) {}
}
