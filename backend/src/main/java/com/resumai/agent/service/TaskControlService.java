package com.resumai.agent.service;

import com.resumai.agent.api.dto.TaskControlRequest;
import com.resumai.agent.api.dto.TaskControlResponse;
import com.resumai.agent.api.ApiConflictException;
import com.resumai.agent.api.ApiNotFoundException;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.QueueStatus;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class TaskControlService {

    private static final Logger log = LoggerFactory.getLogger(TaskControlService.class);
    private static final Set<String> TERMINAL = Set.of(
            "SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "SUPERSEDED");
    private static final Set<String> CANCELLABLE = Set.of(
            "QUEUED", "RUNNING", "PAUSING", "PAUSED", "RESUMING");

    private final ResumeEvaluationService evaluationService;
    private final TaskQueueService taskQueueService;
    private final WorkflowClient workflowClient;

    public TaskControlService(ResumeEvaluationService evaluationService,
                              TaskQueueService taskQueueService,
                              WorkflowClient workflowClient) {
        this.evaluationService = evaluationService;
        this.taskQueueService = taskQueueService;
        this.workflowClient = workflowClient;
    }

    public TaskControlResponse control(String traceId, TaskControlRequest.Action action) {
        ResumeTask row = evaluationService.loadResumeTaskRow(traceId)
                .orElseThrow(() -> new ApiNotFoundException("任务不存在：" + traceId));
        String current = row.getStatus();
        String runId = StringUtils.hasText(row.getWorkflowRunId()) ? row.getWorkflowRunId() : row.getTraceId();

        if (TERMINAL.contains(current)) {
            if (action == TaskControlRequest.Action.CANCEL && "CANCELLED".equals(current)) {
                return response(row, "CANCEL", "CANCELLED", "任务已取消。", runId);
            }
            throw new ApiConflictException("任务状态 " + current + " 不支持 " + action);
        }

        return switch (action) {
            case PAUSE -> pause(row, runId);
            case RESUME -> resume(row, runId);
            case CANCEL -> cancel(row, runId);
        };
    }

    private TaskControlResponse pause(ResumeTask row, String runId) {
        if ("PAUSED".equals(row.getStatus()) || "PAUSING".equals(row.getStatus())) {
            return response(row, "PAUSE", row.getStatus(), "暂停请求已接收。", runId);
        }
        if (QueueStatus.QUEUED.name().equals(row.getQueueStatus())
                || QueueStatus.RETRYING.name().equals(row.getQueueStatus())) {
            if (!evaluationService.compareAndSetControlState(
                    row.getTraceId(), Set.of("QUEUED"), "PAUSED", QueueStatus.PAUSED.name(),
                    "任务尚未开始，已暂停在队列入口。")) {
                return resolveLostTransition(
                        row, "PAUSE", Set.of("PAUSING", "PAUSED"), runId,
                        "暂停请求已由并发操作处理。");
            }
            return response(row, "PAUSE", "PAUSED", "已暂停；继续后会重新入队。", runId);
        }
        if (!"RUNNING".equals(row.getStatus())) {
            throw new ApiConflictException("任务状态 " + row.getStatus() + " 不支持 PAUSE");
        }
        if (!evaluationService.compareAndSetControlState(
                row.getTraceId(), Set.of("RUNNING"), "PAUSING", QueueStatus.RUNNING.name(),
                "正在安全暂停：当前节点结束后写入 checkpoint。")) {
            return resolveLostTransition(
                    row, "PAUSE", Set.of("PAUSING", "PAUSED"), runId,
                    "暂停请求已由并发操作处理。");
        }
        try {
            workflowClient.controlWorkflow(
                    runId, "PAUSE", row.getTraceId(), row.getConversationId(), row.getRevisionNo());
        } catch (Exception e) {
            boolean reverted = evaluationService.compareAndSetControlState(
                    row.getTraceId(), Set.of("PAUSING"), "RUNNING", QueueStatus.RUNNING.name(),
                    "暂停请求未送达 runtime，任务仍在运行。");
            if (!reverted) {
                ResumeTask latest = reload(row.getTraceId());
                if ("PAUSED".equals(latest.getStatus())) {
                    return response(latest, "PAUSE", "PAUSED", "已在 checkpoint 安全暂停。", runId);
                }
            }
            throw new IllegalStateException("暂停请求失败，任务仍在运行：" + e.getMessage(), e);
        }
        return response(row, "PAUSE", "PAUSING", "正在安全暂停，当前节点结束后生效。", runId);
    }

    private TaskControlResponse resume(ResumeTask row, String runId) {
        if ("RUNNING".equals(row.getStatus())) {
            return response(row, "RESUME", "RUNNING", "任务已在运行，无需重复恢复。", runId);
        }
        if ("RESUMING".equals(row.getStatus())) {
            return response(row, "RESUME", "RESUMING", "任务正在从 checkpoint 恢复。", runId);
        }
        if ("PAUSING".equals(row.getStatus())) {
            return resumeRuntimeTask(row, runId, "PAUSING", "暂停请求已撤回，任务继续运行。");
        }
        if (!"PAUSED".equals(row.getStatus())) {
            throw new ApiConflictException("只有 PAUSED 任务可以继续，当前状态：" + row.getStatus());
        }
        if (row.getStartedAt() == null) {
            if (!evaluationService.compareAndSetControlState(
                    row.getTraceId(), Set.of("PAUSED"), "QUEUED", QueueStatus.QUEUED.name(),
                    "任务已恢复并重新进入队列。")) {
                return resolveLostTransition(
                        row, "RESUME", Set.of("QUEUED", "RUNNING", "RESUMING"), runId,
                        "恢复请求已由并发操作处理。");
            }
            try {
                taskQueueService.enqueue(
                        row.getTraceId(), row.getId(), row.getTenantId(), row.getUploadedBy(),
                        row.getPriority() != null ? row.getPriority() : 0);
            } catch (RuntimeException e) {
                evaluationService.compareAndSetControlState(
                        row.getTraceId(), Set.of("QUEUED"), "PAUSED", QueueStatus.PAUSED.name(),
                        "重新入队失败，任务仍保持暂停。");
                throw e;
            }
            return response(row, "RESUME", "QUEUED", "已恢复并重新入队。", runId);
        }
        return resumeRuntimeTask(row, runId, "PAUSED", "已从当前 revision 的 checkpoint 继续运行。");
    }

    private TaskControlResponse resumeRuntimeTask(ResumeTask row, String runId,
                                                  String expectedStatus, String successMessage) {
        if (!evaluationService.compareAndSetControlState(
                row.getTraceId(), Set.of(expectedStatus), "RESUMING", QueueStatus.RESUMING.name(),
                "正在从当前 revision 的 checkpoint 恢复。")) {
            return resolveLostTransition(
                    row, "RESUME", Set.of("RUNNING", "RESUMING"), runId,
                    "恢复请求已由并发操作处理。");
        }
        try {
            workflowClient.controlWorkflow(
                    runId, "RESUME", row.getTraceId(), row.getConversationId(), row.getRevisionNo());
        } catch (Exception e) {
            evaluationService.compareAndSetControlState(
                    row.getTraceId(), Set.of("RESUMING"), expectedStatus,
                    "PAUSING".equals(expectedStatus) ? QueueStatus.RUNNING.name() : QueueStatus.PAUSED.name(),
                    "恢复 checkpoint 失败，任务保持原状态。" );
            throw new IllegalStateException("恢复 checkpoint 失败：" + e.getMessage(), e);
        }
        boolean transitioned = evaluationService.compareAndSetControlState(
                row.getTraceId(), Set.of("RESUMING"), "RUNNING", QueueStatus.RUNNING.name(),
                successMessage);
        if (!transitioned) {
            return resolveLostTransition(
                    row, "RESUME", Set.of("RUNNING", "SUCCESS", "PARTIAL_SUCCESS"), runId,
                    "恢复已完成或任务已结束。");
        }
        return response(row, "RESUME", "RUNNING", successMessage, runId);
    }

    private TaskControlResponse cancel(ResumeTask row, String runId) {
        if ("CANCELLED".equals(row.getStatus())) {
            return response(row, "CANCEL", "CANCELLED", "任务已取消。", runId);
        }
        if (!evaluationService.compareAndSetControlState(
                row.getTraceId(), CANCELLABLE, "CANCELLED", QueueStatus.CANCELLED.name(),
                "任务已由用户取消。")) {
            return resolveLostTransition(
                    row, "CANCEL", Set.of("CANCELLED"), runId,
                    "取消请求已由并发操作处理。");
        }
        tryControl(row, runId, "CANCEL");
        return response(row, "CANCEL", "CANCELLED", "已立即取消；迟到结果会被丢弃。", runId);
    }

    private TaskControlResponse resolveLostTransition(ResumeTask original,
                                                      String action,
                                                      Set<String> idempotentStatuses,
                                                      String runId,
                                                      String message) {
        ResumeTask latest = reload(original.getTraceId());
        if (idempotentStatuses.contains(latest.getStatus())) {
            return response(latest, action, latest.getStatus(), message, runId);
        }
        throw new ApiConflictException(
                "任务状态已由并发操作更新为 " + latest.getStatus() + "，无法执行 " + action);
    }

    private ResumeTask reload(String traceId) {
        return evaluationService.loadResumeTaskRow(traceId)
                .orElseThrow(() -> new ApiNotFoundException("任务不存在：" + traceId));
    }

    private void tryControl(ResumeTask row, String runId, String action) {
        try {
            workflowClient.controlWorkflow(
                    runId, action, row.getTraceId(), row.getConversationId(), row.getRevisionNo());
        } catch (Exception e) {
            // Java status is authoritative. A queued run needs no Python process;
            // a running run will still be protected by the late-result guard.
            log.info("workflow control deferred run={} action={}: {}", runId, action, e.getMessage());
        }
    }

    private TaskControlResponse response(ResumeTask row, String action, String status,
                                         String message, String runId) {
        return new TaskControlResponse(row.getTraceId(), runId, action, status, message);
    }
}
