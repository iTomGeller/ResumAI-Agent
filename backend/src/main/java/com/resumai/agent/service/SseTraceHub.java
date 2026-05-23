package com.resumai.agent.service;

import com.resumai.agent.api.dto.TraceEventResponse;
import java.io.IOException;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * Trace SSE 推送中心。
 *
 * <p>按 TraceId 管理前端连接，并在 Agent 执行阶段将事件实时推送给
 * AgentTerminal。连接断开时会自动清理，避免内存泄漏。</p>
 */
@Component
public class SseTraceHub {

    private final Map<String, List<SseEmitter>> emitters = new ConcurrentHashMap<>();

    /**
     * 订阅指定 TraceId 的实时事件。
     *
     * @param traceId 全局链路 ID
     * @return SSE 连接
     */
    public SseEmitter subscribe(String traceId) {
        SseEmitter emitter = new SseEmitter(30 * 60 * 1000L);
        emitters.computeIfAbsent(traceId, ignored -> new CopyOnWriteArrayList<>()).add(emitter);
        emitter.onCompletion(() -> remove(traceId, emitter));
        emitter.onTimeout(() -> remove(traceId, emitter));
        emitter.onError(error -> remove(traceId, emitter));
        return emitter;
    }

    /**
     * 推送 Trace 事件。
     *
     * @param event Trace 事件
     */
    public void publish(TraceEventResponse event) {
        List<SseEmitter> traceEmitters = emitters.get(event.traceId());
        if (traceEmitters == null || traceEmitters.isEmpty()) {
            return;
        }
        for (SseEmitter emitter : traceEmitters) {
            try {
                emitter.send(SseEmitter.event()
                        .name("trace")
                        .id(event.spanId())
                        .data(event));
            } catch (IOException | IllegalStateException e) {
                remove(event.traceId(), emitter);
            }
        }
    }

    /**
     * 返回当前所有 Trace 的 SSE 订阅总数，供系统健康指标使用。
     */
    public int getActiveSubscriberCount() {
        return emitters.values().stream().mapToInt(List::size).sum();
    }

    private void remove(String traceId, SseEmitter emitter) {
        List<SseEmitter> traceEmitters = emitters.get(traceId);
        if (traceEmitters == null) {
            return;
        }
        traceEmitters.remove(emitter);
        if (traceEmitters.isEmpty()) {
            emitters.remove(traceId);
        }
    }
}
