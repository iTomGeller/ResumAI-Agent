package com.resumai.agent.config;

import com.resumai.agent.service.TaskQueueRepository;
import com.resumai.agent.service.TaskQueueService;
import com.resumai.agent.service.TaskWorkerService;
import jakarta.annotation.PostConstruct;
import java.time.Duration;
import java.time.LocalDateTime;
import org.springframework.stereotype.Component;

@Component
public class TaskQueueMetricsCollector {

    private final AgentMetrics agentMetrics;
    private final TaskQueueService taskQueueService;
    private final TaskQueueRepository taskQueueRepository;
    private final TaskWorkerService taskWorkerService;

    public TaskQueueMetricsCollector(AgentMetrics agentMetrics,
                                       TaskQueueService taskQueueService,
                                       TaskQueueRepository taskQueueRepository,
                                       TaskWorkerService taskWorkerService) {
        this.agentMetrics = agentMetrics;
        this.taskQueueService = taskQueueService;
        this.taskQueueRepository = taskQueueRepository;
        this.taskWorkerService = taskWorkerService;
    }

    @PostConstruct
    public void registerGauges() {
        agentMetrics.registerTaskQueueDepthGauge(() -> (int) Math.min(Integer.MAX_VALUE, taskQueueService.streamSize()));
        agentMetrics.registerTaskQueuePendingGauge(() -> (int) Math.min(Integer.MAX_VALUE, taskQueueService.pendingCount()));
        agentMetrics.registerTaskQueueOldestWaitGauge(this::oldestWaitSeconds);
    }

    private double oldestWaitSeconds() {
        LocalDateTime oldest = taskQueueRepository.oldestQueuedAt();
        if (oldest == null) {
            return 0D;
        }
        return Duration.between(oldest, LocalDateTime.now()).toSeconds();
    }

    public int activeWorkers() {
        return taskWorkerService.getActiveWorkers();
    }
}
