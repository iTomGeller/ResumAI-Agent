package com.resumai.agent.service;

import com.resumai.agent.config.TaskQueueProperties;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.QueueStatus;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.redisson.api.StreamMessageId;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class TaskWorkerService {

    private static final Logger log = LoggerFactory.getLogger(TaskWorkerService.class);

    private final TaskQueueService taskQueueService;
    private final TaskQueueRepository taskQueueRepository;
    private final TaskQueueProperties properties;
    private final ResumeEvaluationService evaluationService;

    private final AtomicInteger activeWorkers = new AtomicInteger(0);
    private final ThreadPoolExecutor workerPool;
    private final Semaphore workerPermits;
    private ExecutorService poller;
    private volatile boolean running = true;
    private volatile long lastRecoveryAt = 0L;

    public TaskWorkerService(TaskQueueService taskQueueService,
                             TaskQueueRepository taskQueueRepository,
                             TaskQueueProperties properties,
                             ResumeEvaluationService evaluationService) {
        this.taskQueueService = taskQueueService;
        this.taskQueueRepository = taskQueueRepository;
        this.properties = properties;
        this.evaluationService = evaluationService;
        int maxWorkers = Math.max(1, properties.getMaxWorkers());
        this.workerPool = (ThreadPoolExecutor) Executors.newFixedThreadPool(maxWorkers);
        this.workerPermits = new Semaphore(maxWorkers, true);
    }

    @PostConstruct
    public void start() {
        if (!properties.isEnabled()) {
            log.warn("Task worker disabled by configuration");
            return;
        }
        recoverStuckTasks();
        poller = Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "task-queue-poller");
            t.setDaemon(true);
            return t;
        });
        poller.submit(this::pollLoop);
        log.info("Task worker started with maxWorkers={} workerId={}", properties.getMaxWorkers(), properties.getWorkerId());
    }

    @PreDestroy
    public void stop() {
        running = false;
        if (poller != null) {
            poller.shutdownNow();
        }
        workerPool.shutdownNow();
    }

    public int getActiveWorkers() {
        return activeWorkers.get();
    }

    private void pollLoop() {
        while (running) {
            try {
                maybeRecoverStuckTasks();
                requeueRetryReadyTasks();
                // Reserve execution capacity before XREADGROUP. Reading first
                // and checking capacity afterwards strands the already claimed
                // message in the PEL whenever all workers are busy.
                if (!workerPermits.tryAcquire()) {
                    Thread.sleep(properties.getPollIntervalMs());
                    continue;
                }
                final java.util.Optional<TaskQueueService.QueuedTaskMessage> messageOpt;
                try {
                    messageOpt = taskQueueService.pollMessage();
                } catch (RuntimeException pollFailure) {
                    workerPermits.release();
                    throw pollFailure;
                }
                if (messageOpt.isEmpty()) {
                    workerPermits.release();
                    Thread.sleep(properties.getPollIntervalMs());
                    continue;
                }
                TaskQueueService.QueuedTaskMessage message = messageOpt.get();
                try {
                    workerPool.submit(() -> processMessage(message));
                } catch (RuntimeException submitFailure) {
                    workerPermits.release();
                    throw submitFailure;
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception e) {
                log.warn("Task queue poll failed: {}", e.getMessage());
                sleepQuietly(properties.getPollIntervalMs());
            }
        }
    }

    private void processMessage(TaskQueueService.QueuedTaskMessage message) {
        activeWorkers.incrementAndGet();
        long waitStart = System.currentTimeMillis();
        StreamMessageId messageId = message.messageId();
        String traceId = message.traceId();
        try {
            int claimed = taskQueueRepository.claimTask(traceId, properties.getWorkerId());
            if (claimed == 0) {
                taskQueueService.ack(messageId);
                return;
            }
            evaluationService.runQueuedEvaluation(traceId);
            taskQueueService.ack(messageId);
        } catch (Exception e) {
            log.warn("Task execution failed trace={}: {}", traceId, e.getMessage());
            handleFailure(traceId, messageId, e);
        } finally {
            activeWorkers.decrementAndGet();
            workerPermits.release();
        }
    }

    private void handleFailure(String traceId, StreamMessageId messageId, Exception e) {
        ResumeTask row = evaluationService.loadResumeTaskRow(traceId).orElse(null);
        int attempts = row != null && row.getAttemptCount() != null ? row.getAttemptCount() + 1 : 1;
        if (attempts >= properties.getMaxAttempts()) {
            taskQueueRepository.markQueueFailed(traceId, e.getMessage());
            taskQueueService.ack(messageId);
            return;
        }
        LocalDateTime nextRetry = LocalDateTime.now().plusSeconds((long) properties.getRetryBackoffSeconds() * attempts);
        taskQueueRepository.markRetrying(traceId, attempts, nextRetry);
        taskQueueService.ack(messageId);
        taskQueueService.enqueue(traceId,
                row != null ? row.getId() : null,
                row != null ? row.getTenantId() : "default",
                row != null ? row.getUploadedBy() : "demo-hr",
                row != null && row.getPriority() != null ? row.getPriority() : 0);
    }

    private void maybeRecoverStuckTasks() {
        long now = System.currentTimeMillis();
        if (now - lastRecoveryAt < 60_000L) {
            return;
        }
        lastRecoveryAt = now;
        recoverStuckTasks();
    }

    private void recoverStuckTasks() {
        LocalDateTime cutoff = LocalDateTime.now().minusMinutes(properties.getRunningTimeoutMinutes());
        int recovered = taskQueueRepository.recoverStuckRunning(cutoff);
        if (recovered > 0) {
            log.info("Recovered {} stuck RUNNING tasks", recovered);
        }
        List<ResumeTask> retryRows = taskQueueRepository.listStuckForRequeue(LocalDateTime.now(), 100);
        for (ResumeTask row : retryRows) {
            if (StringUtils.hasText(row.getTraceId())) {
                taskQueueService.enqueue(row.getTraceId(), row.getId(), row.getTenantId(), row.getUploadedBy(),
                        row.getPriority() != null ? row.getPriority() : 0);
            }
        }
    }

    private void requeueRetryReadyTasks() {
        List<ResumeTask> rows = taskQueueRepository.listStuckForRequeue(LocalDateTime.now(), 20);
        for (ResumeTask row : rows) {
            if (QueueStatus.RETRYING.name().equals(row.getQueueStatus())) {
                taskQueueService.enqueue(row.getTraceId(), row.getId(), row.getTenantId(), row.getUploadedBy(),
                        row.getPriority() != null ? row.getPriority() : 0);
            }
        }
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
