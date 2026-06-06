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

/**
 * Captures multi-agent execution trace events and bridges them to:
 * 1. OpenTelemetry spans → Langfuse
 * 2. In-memory buffer → agent_execution_trace table (frontend)
 * 3. SSE push → real-time frontend updates
 */
@Component
public class AgentTraceCapture {

    private static final Logger log = LoggerFactory.getLogger(AgentTraceCapture.class);
    private final Tracer tracer;

    private final Map<String, TraceSession> activeSessions = new ConcurrentHashMap<>();

    public AgentTraceCapture(OpenTelemetry openTelemetry) {
        this.tracer = openTelemetry.getTracer("resumai-agent-orchestrator");
    }

    public void begin(String traceId) {
        TraceSession session = new TraceSession(traceId);
        activeSessions.put(traceId, session);
        log.debug("Trace session started: {}", traceId);
    }

    public void agentStart(String agentName, String description, int phase) {
        String threadTraceId = findActiveTraceId();
        if (threadTraceId == null) return;

        TraceSession session = activeSessions.get(threadTraceId);
        if (session == null) return;

        Span agentSpan = tracer.spanBuilder(agentName)
                .setParent(Context.current())
                .setAttribute("langfuse.observation.type", "span")
                .setAttribute("langfuse.observation.name", agentName)
                .setAttribute(AttributeKey.stringKey("agent.name"), agentName)
                .setAttribute(AttributeKey.stringKey("agent.description"), description)
                .setAttribute(AttributeKey.longKey("agent.phase"), phase)
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

    public void agentEnd(String agentName, String status, long durationMs, String output) {
        String threadTraceId = findActiveTraceId();
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

        for (AgentEvent event : session.events) {
            if (event.agentName.equals(agentName) && "RUNNING".equals(event.status)) {
                event.status = status;
                event.durationMs = durationMs;
                event.output = output != null && output.length() > 500 ? output.substring(0, 500) + "..." : output;
                break;
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
        return session != null ? session.events : List.of();
    }

    public List<AgentEvent> consumeEvents(String traceId) {
        TraceSession session = completedSessions.remove(traceId);
        if (session == null) session = activeSessions.get(traceId);
        return session != null ? new ArrayList<>(session.events) : List.of();
    }

    private final Map<String, TraceSession> completedSessions = new ConcurrentHashMap<>();

    private String findActiveTraceId() {
        for (Map.Entry<String, TraceSession> entry : activeSessions.entrySet()) {
            if ("RUNNING".equals(entry.getValue().status) || entry.getValue().status == null) {
                return entry.getKey();
            }
        }
        return activeSessions.keySet().stream().findFirst().orElse(null);
    }

    public static class TraceSession {
        final String traceId;
        final List<AgentEvent> events = Collections.synchronizedList(new ArrayList<>());
        final Map<String, Deque<Span>> spanStacks = new ConcurrentHashMap<>();
        String status;
        long totalDurationMs;

        TraceSession(String traceId) {
            this.traceId = traceId;
        }

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
    }
}
