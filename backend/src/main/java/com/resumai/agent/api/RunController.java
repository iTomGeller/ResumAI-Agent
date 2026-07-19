package com.resumai.agent.api;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.HumanFeedbackLogMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.HumanFeedbackLog;
import com.resumai.agent.domain.enums.RunStatus;
import com.resumai.agent.service.MemoryService;
import com.resumai.agent.service.run.PolicyService;
import com.resumai.agent.service.run.RewardService;
import com.resumai.agent.service.run.RunEventService;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunQueueService;
import com.resumai.agent.service.run.RunSchedulerService;
import com.resumai.agent.util.HrContext;
import java.time.LocalDateTime;
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

/** Public run API: status, SSE stream with replay, cancel, feedback, policy stats. */
@RestController
@RequestMapping
public class RunController {

    private final RunLifecycleService lifecycleService;
    private final RunQueueService queueService;
    private final RunEventService eventService;
    private final RunSchedulerService schedulerService;
    private final RewardService rewardService;
    private final PolicyService policyService;
    private final MemoryService memoryService;
    private final HumanFeedbackLogMapper feedbackMapper;
    private final com.resumai.agent.dao.SandboxExecutionMapper sandboxExecutionMapper;
    private final ObjectMapper objectMapper;

    public RunController(RunLifecycleService lifecycleService,
                         RunQueueService queueService,
                         RunEventService eventService,
                         RunSchedulerService schedulerService,
                         RewardService rewardService,
                         PolicyService policyService,
                         MemoryService memoryService,
                         HumanFeedbackLogMapper feedbackMapper,
                         com.resumai.agent.dao.SandboxExecutionMapper sandboxExecutionMapper,
                         ObjectMapper objectMapper) {
        this.lifecycleService = lifecycleService;
        this.queueService = queueService;
        this.eventService = eventService;
        this.schedulerService = schedulerService;
        this.rewardService = rewardService;
        this.policyService = policyService;
        this.memoryService = memoryService;
        this.feedbackMapper = feedbackMapper;
        this.sandboxExecutionMapper = sandboxExecutionMapper;
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

    public record RunFeedbackRequest(Integer ratingScore, String comment, String fixAction,
                                     Boolean accepted, Boolean recommendationAgreed,
                                     Double scoreDelta, Integer missedEvidenceCount,
                                     Integer unsupportedClaimCount, Boolean riskJudgementCorrect) {
    }

    /** HR feedback → reward → policy statistics (the learning loop entry). */
    @PostMapping("/api/runs/{runId}/feedback")
    public Map<String, Object> submitFeedback(@PathVariable String runId,
                                              @RequestBody RunFeedbackRequest request) {
        AgentRun run = lifecycleService.getRun(runId);
        if (run == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        int rating = request.ratingScore() != null
                ? Math.max(1, Math.min(5, request.ratingScore())) : 3;
        Map<String, Object> structured = new LinkedHashMap<>();
        if (request.accepted() != null) {
            structured.put("accepted", request.accepted());
        }
        if (request.recommendationAgreed() != null) {
            structured.put("recommendationAgreed", request.recommendationAgreed());
        }
        if (request.scoreDelta() != null) {
            structured.put("scoreDelta", request.scoreDelta());
        }
        if (request.missedEvidenceCount() != null) {
            structured.put("missedEvidenceCount", request.missedEvidenceCount());
        }
        if (request.unsupportedClaimCount() != null) {
            structured.put("unsupportedClaimCount", request.unsupportedClaimCount());
        }
        if (request.riskJudgementCorrect() != null) {
            structured.put("riskJudgementCorrect", request.riskJudgementCorrect());
        }

        HumanFeedbackLog feedback = new HumanFeedbackLog();
        feedback.setTraceId(run.getTraceId());
        feedback.setRunId(runId);
        feedback.setPolicyId(run.getPolicyId());
        feedback.setRatingScore(rating);
        feedback.setHumanComment(request.comment());
        feedback.setFixAction(request.fixAction());
        feedback.setFeedbackType(rating >= 4 ? "APPROVE" : rating <= 2 ? "REJECT" : "REVIEW");
        feedback.setReviewer(HrContext.getHrId());
        feedback.setAdopted(Boolean.TRUE.equals(request.accepted()) ? 1 : 0);
        feedback.setStructuredPayload(writeJson(structured));
        feedback.setCreateTime(LocalDateTime.now());
        feedback.setUpdateTime(LocalDateTime.now());
        feedback.setDeleted(0);
        feedbackMapper.insert(feedback);

        double reward = rewardService.recordFeedbackReward(
                run, feedback.getId(), rating, objectMapper.valueToTree(structured));
        memoryService.writeHrFeedbackMemory(run, request.comment(), structured);
        return Map.of(
                "status", "OK",
                "feedbackId", feedback.getId(),
                "reward", Math.round(reward * 10000.0) / 10000.0,
                "policyId", run.getPolicyId() != null ? run.getPolicyId() : "");
    }

    @GetMapping("/api/runs/{runId}/sandbox-executions")
    public Object sandboxExecutions(@PathVariable String runId) {
        return sandboxExecutionMapper.selectList(
                new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<
                        com.resumai.agent.domain.entity.SandboxExecutionRow>()
                        .eq("run_id", runId)
                        .orderByAsc("id"));
    }

    @GetMapping("/api/policies/statistics")
    public Object policyStatistics(@RequestParam(value = "taskCategory", required = false)
                                   String taskCategory) {
        return policyService.listStatistics(taskCategory);
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
        view.put("policyId", run.getPolicyId());
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

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "{}";
        }
    }
}
