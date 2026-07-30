package com.resumai.agent.api;

import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.TaskListItemResponse;
import com.resumai.agent.api.dto.TaskQueueStatusResponse;
import com.resumai.agent.config.TaskQueueProperties;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.QueueStatus;
import com.resumai.agent.service.TaskQueueRepository;
import com.resumai.agent.service.TaskQueueService;
import com.resumai.agent.service.TaskWorkerService;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import org.springframework.util.StringUtils;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/task-queue")
public class TaskQueueController {

    private final TaskQueueRepository taskQueueRepository;
    private final TaskQueueService taskQueueService;
    private final TaskWorkerService taskWorkerService;
    private final TaskQueueProperties properties;
    private final com.resumai.agent.service.ResumeEvaluationService evaluationService;

    public TaskQueueController(TaskQueueRepository taskQueueRepository,
                               TaskQueueService taskQueueService,
                               TaskWorkerService taskWorkerService,
                               TaskQueueProperties properties,
                               com.resumai.agent.service.ResumeEvaluationService evaluationService) {
        this.taskQueueRepository = taskQueueRepository;
        this.taskQueueService = taskQueueService;
        this.taskWorkerService = taskWorkerService;
        this.properties = properties;
        this.evaluationService = evaluationService;
    }

    @GetMapping("/status")
    public TaskQueueStatusResponse status() {
        LocalDateTime oldest = taskQueueRepository.oldestQueuedAt();
        long oldestWait = oldest == null ? 0L : Duration.between(oldest, LocalDateTime.now()).toSeconds();
        int capacity = Math.max(1, properties.getMaxWorkers());
        int active = taskWorkerService.getActiveWorkers();
        List<String> recentFailures = taskQueueRepository.listByQueueStatus(QueueStatus.FAILED.name(), 5).stream()
                .map(row -> row.getFileName() + ": " + (StringUtils.hasText(row.getFailReason()) ? row.getFailReason() : "unknown"))
                .toList();
        LocalDateTime runningCutoff = LocalDateTime.now().minusMinutes(properties.getRunningTimeoutMinutes());
        return new TaskQueueStatusResponse(
                taskQueueRepository.countByQueueStatus(QueueStatus.QUEUED.name()),
                taskQueueRepository.countRunningFresh(runningCutoff),
                taskQueueRepository.countByQueueStatus(QueueStatus.RETRYING.name()),
                taskQueueRepository.countByQueueStatus(QueueStatus.FAILED.name()),
                taskQueueRepository.countStuckRunning(runningCutoff),
                taskQueueService.pendingCount(),
                oldestWait,
                active,
                capacity,
                (double) active / capacity,
                recentFailures
        );
    }

    @GetMapping("/tasks")
    public PageResult<TaskListItemResponse> queueTasks(
            @RequestParam(defaultValue = "RUNNING") String status,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        List<ResumeTask> rows = taskQueueRepository.listByQueueStatus(status.toUpperCase(), pageSize);
        List<TaskListItemResponse> items = rows.stream()
                .map(evaluationService::toListItemFromEntity)
                .toList();
        return PageResult.of(items, items.size(), page, pageSize);
    }
}
