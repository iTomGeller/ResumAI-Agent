package com.resumai.agent.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.enums.RunStatus;
import com.resumai.agent.service.run.RunEventService;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunQueueService;
import com.resumai.agent.service.run.RunSchedulerService;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/** Public run API: status, SSE stream with replay, pause, resume and cancel. */
@RestController
@RequestMapping
public class RunController {

    private final RunLifecycleService lifecycleService;
    private final RunQueueService queueService;
    private final RunEventService eventService;
    private final RunSchedulerService schedulerService;
    private final ObjectMapper objectMapper;

    public RunController(RunLifecycleService lifecycleService,
                         RunQueueService queueService,
                         RunEventService eventService,
                         RunSchedulerService schedulerService,
                         ObjectMapper objectMapper) {
        this.lifecycleService = lifecycleService;
        this.queueService = queueService;
        this.eventService = eventService;
        this.schedulerService = schedulerService;
        this.objectMapper = objectMapper;
    }

    @GetMapping("/api/runs/{runId}")
    public Map<String, Object> getRun(@PathVariable String runId) {
        AgentRun run = lifecycleService.getRun(runId);
        if (run == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        return toView(run);
    }

    public record CancelRequest(String reason) {
    }

    @PostMapping("/api/runs/{runId}/cancel")
    public Map<String, Object> cancelRun(@PathVariable String runId,
                                         @RequestBody(required = false) CancelRequest request) {
        AgentRun run = lifecycleService.getRun(runId);
        if (run == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        String reason = request != null && request.reason() != null
                ? request.reason() : "用户点击停止";
        if (RunStatus.QUEUED.name().equals(run.getStatus())) {
            queueService.cancelQueued(runId, reason);
            eventService.publish(runId, run.getConversationId(), run.getTraceId(),
                    "run.cancelled", null, null, Map.of("reason", reason));
        } else if (!RunStatus.isTerminal(run.getStatus())) {
            lifecycleService.cancelActiveRun(run, "user_cancelled", reason);
        }
        return toView(lifecycleService.getRun(runId));
    }

    public record PauseRequest(String reason) {
    }

    @PostMapping("/api/runs/{runId}/pause")
    public Map<String, Object> pauseRun(@PathVariable String runId,
                                        @RequestBody(required = false) PauseRequest request) {
        AgentRun run = lifecycleService.getRun(runId);
        if (run == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        if (RunStatus.isTerminal(run.getStatus()) || RunStatus.isPaused(run.getStatus())) {
            return toView(run);
        }
        String reason = request != null && request.reason() != null
                ? request.reason() : "用户请求暂停";
        return toView(lifecycleService.pauseActiveRun(run, reason));
    }

    @PostMapping("/api/runs/{runId}/resume")
    public Map<String, Object> resumeRun(@PathVariable String runId) {
        AgentRun run = lifecycleService.getRun(runId);
        if (run == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        if (!RunStatus.isPaused(run.getStatus())) {
            return toView(run);
        }
        return toView(lifecycleService.resumePausedRun(run));
    }

    /** Retry a FAILED/TIMED_OUT run from its last group-boundary checkpoint. */
    @PostMapping("/api/runs/{runId}/retry")
    public Map<String, Object> retryRun(@PathVariable String runId) {
        AgentRun retry = lifecycleService.retryFromCheckpoint(runId);
        schedulerService.kick();
        return toView(retry);
    }

    @GetMapping(value = "/sse/runs/{runId}", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter streamRun(@PathVariable String runId,
                                @RequestParam(value = "afterSeq", defaultValue = "0") int afterSeq,
                                @RequestHeader(value = "Last-Event-ID", required = false)
                                String lastEventId) {
        int replayFrom = afterSeq;
        if (lastEventId != null && lastEventId.contains(":")) {
            try {
                replayFrom = Integer.parseInt(
                        lastEventId.substring(lastEventId.lastIndexOf(':') + 1));
            } catch (NumberFormatException ignored) {
                // fall back to afterSeq param
            }
        }
        return eventService.subscribeRun(runId, replayFrom);
    }

    @GetMapping(value = "/sse/conversations/{conversationId}",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter streamConversation(@PathVariable String conversationId) {
        return eventService.subscribeConversation(conversationId);
    }

    @GetMapping("/api/runs/queue/status")
    public Map<String, Object> queueStatus() {
        Map<String, Object> snapshot = new LinkedHashMap<>(queueService.queueSnapshot());
        snapshot.put("schedulerKicked", true);
        schedulerService.kick();
        return snapshot;
    }

    private Map<String, Object> toView(AgentRun run) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("runId", run.getRunId());
        view.put("conversationId", run.getConversationId());
        view.put("userId", run.getUserId());
        view.put("traceId", run.getTraceId());
        view.put("revision", run.getRevisionNo());
        view.put("runType", run.getRunType());
        view.put("queueMode", run.getQueueMode());
        view.put("status", run.getStatus());
        view.put("queuePosition", queueService.queuePosition(run));
        view.put("currentAgent", run.getCurrentAgent());
        view.put("currentTool", run.getCurrentTool());
        view.put("currentPhase", run.getCurrentPhase());
        view.put("answer", run.getAnswer());
        view.put("errorCode", run.getErrorCode());
        view.put("errorMessage", run.getErrorMessage());
        view.put("cancellationReason", run.getCancellationReason());
        view.put("metrics", readJson(run.getMetrics()));
        view.put("createdAt", String.valueOf(run.getCreatedAt()));
        view.put("startedAt", String.valueOf(run.getStartedAt()));
        view.put("finishedAt", String.valueOf(run.getFinishedAt()));
        view.put("timeoutAt", String.valueOf(run.getTimeoutAt()));
        return view;
    }

    private Object readJson(String json) {
        try {
            return json != null ? objectMapper.readValue(json, Map.class) : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

}
