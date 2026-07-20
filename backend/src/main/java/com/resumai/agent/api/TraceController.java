package com.resumai.agent.api;

import com.resumai.agent.api.dto.TraceEventResponse;
import com.resumai.agent.service.ResumeEvaluationService;
import com.resumai.agent.service.SseTraceHub;
import com.resumai.agent.service.run.RunTraceBridgeService;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * Agent Trace 控制器。
 *
 * <p>提供历史 Trace 查询和 SSE 实时订阅，支撑前端 AgentTerminal 瀑布流。
 * 统一 Runtime 的运行事件由 {@link RunTraceBridgeService} 渲染进同一视图。</p>
 */
@RestController
@RequestMapping
public class TraceController {

    private final ResumeEvaluationService evaluationService;
    private final SseTraceHub sseTraceHub;
    private final RunTraceBridgeService traceBridge;

    public TraceController(ResumeEvaluationService evaluationService,
                           SseTraceHub sseTraceHub,
                           RunTraceBridgeService traceBridge) {
        this.evaluationService = evaluationService;
        this.sseTraceHub = sseTraceHub;
        this.traceBridge = traceBridge;
    }

    /**
     * 查询指定 TraceId 的历史事件。
     *
     * <p>合并两个来源：任务层写入的 legacy 事件（如 TASK_CREATED）与统一
     * Runtime 的运行事件（经桥接渲染），按时间排序后返回。</p>
     *
     * @param traceId TraceId
     * @return Trace 事件
     */
    @GetMapping("/api/traces/{traceId}")
    public List<TraceEventResponse> listTraces(@PathVariable String traceId) {
        List<TraceEventResponse> merged = new java.util.ArrayList<>(
                evaluationService.listTraces(traceId));
        merged.addAll(traceBridge.traceEventsForTrace(traceId));
        merged.sort(java.util.Comparator.comparing(
                TraceEventResponse::timestamp,
                java.util.Comparator.nullsLast(java.util.Comparator.naturalOrder())));
        return merged;
    }

    /**
     * 订阅指定 TraceId 的实时事件。
     *
     * @param traceId TraceId
     * @return SSE 连接
     */
    @GetMapping("/sse/traces/{traceId}")
    public SseEmitter subscribe(@PathVariable String traceId) {
        return sseTraceHub.subscribe(traceId);
    }
}
