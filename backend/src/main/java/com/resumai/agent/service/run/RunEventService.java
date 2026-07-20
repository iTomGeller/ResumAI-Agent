package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.RunEventMapper;
import com.resumai.agent.domain.entity.RunEvent;
import com.resumai.agent.service.SseTraceHub;
import java.io.IOException;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import org.redisson.api.RedissonClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Lazy;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * Persists every run event (SSE replay source) and fans it out to live
 * subscribers keyed by runId and conversationId. Sequence numbers come from a
 * Redis counter so ordering survives multi-instance deployments; MySQL keeps
 * the durable copy used to rebuild client state after an SSE reconnect.
 */
@Service
public class RunEventService {

    private static final Logger log = LoggerFactory.getLogger(RunEventService.class);
    private static final long EMITTER_TIMEOUT_MS = 30L * 60 * 1000;

    private final RunEventMapper runEventMapper;
    private final RedissonClient redisson;
    private final ObjectMapper objectMapper;
    private final SseTraceHub sseTraceHub;
    private final RunTraceBridgeService traceBridge;

    private final Map<String, List<SseEmitter>> runEmitters = new ConcurrentHashMap<>();
    private final Map<String, List<SseEmitter>> conversationEmitters = new ConcurrentHashMap<>();

    public RunEventService(RunEventMapper runEventMapper,
                           RedissonClient redisson,
                           ObjectMapper objectMapper,
                           SseTraceHub sseTraceHub,
                           @Lazy RunTraceBridgeService traceBridge) {
        this.runEventMapper = runEventMapper;
        this.redisson = redisson;
        this.objectMapper = objectMapper;
        this.sseTraceHub = sseTraceHub;
        this.traceBridge = traceBridge;
    }

    public RunEvent publish(String runId, String conversationId, String traceId,
                            String eventType, String agentId, String toolName,
                            Map<String, Object> payload) {
        RunEvent event = new RunEvent();
        event.setRunId(runId);
        event.setConversationId(conversationId);
        event.setTraceId(traceId);
        event.setSeq(nextSeq(runId));
        event.setEventType(eventType);
        event.setAgentId(agentId);
        event.setToolName(toolName);
        event.setPayload(writeJson(payload));
        event.setCreateTime(LocalDateTime.now());
        try {
            runEventMapper.insert(event);
        } catch (Exception e) {
            log.warn("run event persist failed run={} type={}: {}", runId, eventType, e.getMessage());
        }
        fanOut(event);
        relayToTraceView(event);
        return event;
    }

    /** Mirror run events onto the legacy /sse/traces feed (task detail page). */
    private void relayToTraceView(RunEvent event) {
        if (!StringUtils.hasText(event.getTraceId())) {
            return;
        }
        try {
            sseTraceHub.publish(traceBridge.toTraceEvent(event.getTraceId(), event));
        } catch (Exception e) {
            log.debug("trace relay skipped run={} type={}: {}",
                    event.getRunId(), event.getEventType(), e.getMessage());
        }
    }

    private int nextSeq(String runId) {
        try {
            return (int) redisson.getAtomicLong("resumai:run:eventseq:" + runId).incrementAndGet();
        } catch (Exception e) {
            // Redis degraded: fall back to DB max+1 (single instance safe).
            RunEvent last = runEventMapper.selectOne(new QueryWrapper<RunEvent>()
                    .eq("run_id", runId).orderByDesc("seq").last("limit 1"));
            return last != null && last.getSeq() != null ? last.getSeq() + 1 : 1;
        }
    }

    public List<RunEvent> listSince(String runId, int afterSeq, int limit) {
        return runEventMapper.selectList(new QueryWrapper<RunEvent>()
                .eq("run_id", runId)
                .gt("seq", afterSeq)
                .orderByAsc("seq")
                .last("limit " + Math.min(Math.max(limit, 1), 2000)));
    }

    public SseEmitter subscribeRun(String runId, int afterSeq) {
        SseEmitter emitter = new SseEmitter(EMITTER_TIMEOUT_MS);
        register(runEmitters, runId, emitter);
        replay(emitter, runId, afterSeq);
        return emitter;
    }

    public SseEmitter subscribeConversation(String conversationId) {
        SseEmitter emitter = new SseEmitter(EMITTER_TIMEOUT_MS);
        register(conversationEmitters, conversationId, emitter);
        return emitter;
    }

    private void replay(SseEmitter emitter, String runId, int afterSeq) {
        try {
            for (RunEvent event : listSince(runId, afterSeq, 2000)) {
                emitter.send(toSse(event));
            }
        } catch (IOException | IllegalStateException e) {
            emitter.complete();
        }
    }

    private void register(Map<String, List<SseEmitter>> registry, String key, SseEmitter emitter) {
        registry.computeIfAbsent(key, ignored -> new CopyOnWriteArrayList<>()).add(emitter);
        emitter.onCompletion(() -> remove(registry, key, emitter));
        emitter.onTimeout(() -> remove(registry, key, emitter));
        emitter.onError(error -> remove(registry, key, emitter));
    }

    private void fanOut(RunEvent event) {
        deliver(runEmitters.get(event.getRunId()), event, runEmitters, event.getRunId());
        deliver(conversationEmitters.get(event.getConversationId()), event,
                conversationEmitters, event.getConversationId());
    }

    private void deliver(List<SseEmitter> targets, RunEvent event,
                         Map<String, List<SseEmitter>> registry, String key) {
        if (targets == null || targets.isEmpty()) {
            return;
        }
        for (SseEmitter emitter : targets) {
            try {
                emitter.send(toSse(event));
            } catch (IOException | IllegalStateException e) {
                remove(registry, key, emitter);
            }
        }
    }

    private SseEmitter.SseEventBuilder toSse(RunEvent event) {
        return SseEmitter.event()
                .name("run")
                .id(event.getRunId() + ":" + event.getSeq())
                .data(Map.of(
                        "runId", event.getRunId(),
                        "conversationId", event.getConversationId(),
                        "seq", event.getSeq(),
                        "eventType", event.getEventType(),
                        "agentId", event.getAgentId() != null ? event.getAgentId() : "",
                        "toolName", event.getToolName() != null ? event.getToolName() : "",
                        "payload", readJson(event.getPayload()),
                        "createdAt", String.valueOf(event.getCreateTime())));
    }

    private void remove(Map<String, List<SseEmitter>> registry, String key, SseEmitter emitter) {
        List<SseEmitter> list = registry.get(key);
        if (list == null) {
            return;
        }
        list.remove(emitter);
        if (list.isEmpty()) {
            registry.remove(key);
        }
    }

    private String writeJson(Map<String, Object> payload) {
        try {
            return objectMapper.writeValueAsString(payload != null ? payload : Map.of());
        } catch (Exception e) {
            return "{}";
        }
    }

    private Object readJson(String payload) {
        try {
            return payload != null ? objectMapper.readValue(payload, Map.class) : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    public int activeSubscribers() {
        return runEmitters.values().stream().mapToInt(List::size).sum()
                + conversationEmitters.values().stream().mapToInt(List::size).sum();
    }
}
