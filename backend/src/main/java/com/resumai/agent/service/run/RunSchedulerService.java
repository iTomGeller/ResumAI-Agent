package com.resumai.agent.service.run;

import com.resumai.agent.config.AgentRunProperties;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.enums.RunStatus;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Dispatcher + watchdog for conversational runs.
 *
 * <p>Dispatch: pull QUEUED runs (FIFO per conversation), acquire the global
 * expirable permit (max 12 by the measured single-node default) and the
 * conversation permit (max 1),
 * CAS QUEUED→STARTING, then hand off to the lifecycle service. Watchdog:
 * renew permit leases for healthy runs, time out overdue runs, force-close
 * stuck CANCELLING/STARTING runs, and recover orphans after restart.</p>
 */
@Service
public class RunSchedulerService {

    private static final Logger log = LoggerFactory.getLogger(RunSchedulerService.class);

    private final RunQueueService queueService;
    private final RunLifecycleService lifecycleService;
    private final RunPermitService permitService;
    private final AgentRuntimeClient runtimeClient;
    private final AgentRunProperties properties;

    private ScheduledExecutorService scheduler;
    private ExecutorService startPool;
    private volatile boolean running = true;
    /** When true, no new QUEUED→STARTING transitions (deploy drain). */
    private volatile boolean draining = false;

    public RunSchedulerService(RunQueueService queueService,
                               RunLifecycleService lifecycleService,
                               RunPermitService permitService,
                               AgentRuntimeClient runtimeClient,
                               AgentRunProperties properties) {
        this.queueService = queueService;
        this.lifecycleService = lifecycleService;
        this.permitService = permitService;
        this.runtimeClient = runtimeClient;
        this.properties = properties;
    }

    @PostConstruct
    public void start() {
        if (!properties.isEnabled()) {
            log.warn("agent run scheduler disabled by configuration");
            return;
        }
        recoverAfterRestart();
        startPool = Executors.newFixedThreadPool(
                Math.max(2, properties.getMaxGlobalConcurrent()),
                r -> daemon(r, "run-starter"));
        scheduler = Executors.newScheduledThreadPool(2, r -> daemon(r, "run-scheduler"));
        scheduler.scheduleWithFixedDelay(this::safeDispatch,
                properties.getDispatchIntervalMs(), properties.getDispatchIntervalMs(),
                TimeUnit.MILLISECONDS);
        scheduler.scheduleWithFixedDelay(this::safeWatchdog,
                properties.getWatchdogIntervalMs(), properties.getWatchdogIntervalMs(),
                TimeUnit.MILLISECONDS);
        log.info("agent run scheduler started maxGlobal={} dispatchMs={}",
                properties.getMaxGlobalConcurrent(), properties.getDispatchIntervalMs());
    }

    @PreDestroy
    public void stop() {
        running = false;
        if (scheduler != null) {
            scheduler.shutdownNow();
        }
        if (startPool != null) {
            startPool.shutdownNow();
        }
    }

    /** Manual kick (after enqueue or run completion) to reduce dispatch latency. */
    public void kick() {
        if (scheduler != null && running && !draining) {
            scheduler.execute(this::safeDispatch);
        }
    }

    /** Stop accepting new starts (safe deploy drain). */
    public void setDraining(boolean draining) {
        this.draining = draining;
        log.info("scheduler draining={}", draining);
    }

    public boolean isDraining() {
        return draining;
    }

    private void safeDispatch() {
        try {
            dispatchOnce();
        } catch (Exception e) {
            log.warn("dispatch cycle failed: {}", e.getMessage());
        }
    }

    void dispatchOnce() {
        if (!running || draining) {
            return;
        }
        List<AgentRun> queued = queueService.listQueuedGlobal(32);
        if (queued.isEmpty()) {
            return;
        }
        Set<String> visitedConversations = new HashSet<>();
        for (AgentRun run : queued) {
            // FIFO inside one conversation: only its earliest queued run may start.
            if (!visitedConversations.add(run.getConversationId())) {
                continue;
            }
            if (queueService.findActiveRun(run.getConversationId()) != null) {
                continue;
            }
            String globalPermit = permitService.tryAcquireGlobal();
            if (globalPermit == null) {
                return; // at capacity — later conversations must wait
            }
            String convPermit = permitService.tryAcquireConversation(run.getConversationId());
            if (convPermit == null) {
                permitService.releaseGlobal(globalPermit);
                continue;
            }
            if (!lifecycleService.markStarting(run.getRunId(), convPermit, globalPermit)) {
                permitService.releaseConversation(run.getConversationId(), convPermit);
                permitService.releaseGlobal(globalPermit);
                continue;
            }
            startPool.submit(() -> {
                try {
                    lifecycleService.startRun(run.getRunId());
                } catch (Exception e) {
                    log.warn("run start crashed run={}: {}", run.getRunId(), e.getMessage());
                }
            });
        }
    }

    private void safeWatchdog() {
        try {
            watchdogOnce();
        } catch (Exception e) {
            log.warn("watchdog cycle failed: {}", e.getMessage());
        }
    }

    void watchdogOnce() {
        List<AgentRun> active = lifecycleService.listByStatuses(
                List.copyOf(RunStatus.ACTIVE), 200);
        LocalDateTime now = LocalDateTime.now();
        for (AgentRun run : active) {
            permitService.renewLeases(run.getConversationId(),
                    run.getConvPermitId(), run.getGlobalPermitId());

            boolean overdue = run.getTimeoutAt() != null && now.isAfter(run.getTimeoutAt());
            if (overdue && !RunStatus.CANCELLING.name().equals(run.getStatus())) {
                log.warn("run overdue, timing out run={} timeoutAt={}", run.getRunId(), run.getTimeoutAt());
                lifecycleService.forceTerminal(run, RunStatus.TIMED_OUT,
                        "RUN_TIMEOUT", "运行超出总时限，已终止");
                continue;
            }
            if (RunStatus.CANCELLING.name().equals(run.getStatus())
                    && RunLifecycleService.secondsSince(run.getUpdatedAt())
                    > properties.getCancelGraceSeconds()) {
                log.warn("cancel grace exceeded, force closing run={}", run.getRunId());
                lifecycleService.forceTerminal(run, RunStatus.CANCELLED,
                        "CANCEL_FORCED", "取消宽限期已过，强制关闭");
                continue;
            }
            if (RunStatus.PAUSING.name().equals(run.getStatus())
                    && RunLifecycleService.secondsSince(run.getUpdatedAt())
                    > properties.getPauseGraceSeconds()) {
                // no snapshot callback arrived; the runtime kept executing
                log.warn("pause grace exceeded, reverting to RUNNING run={}", run.getRunId());
                lifecycleService.revertPausing(run.getRunId());
                continue;
            }
            if (RunStatus.STARTING.name().equals(run.getStatus())
                    && RunLifecycleService.secondsSince(run.getUpdatedAt())
                    > properties.getStartGraceSeconds()) {
                log.warn("run stuck in STARTING, failing run={}", run.getRunId());
                lifecycleService.forceTerminal(run, RunStatus.FAILED,
                        "START_STUCK", "启动阶段卡死，已失败");
            }
        }

        // PAUSED runs: keep the conversation reserved (serial guarantee) but
        // never forever — expired pauses converge to CANCELLED.
        for (AgentRun paused : lifecycleService.listByStatuses(
                List.of(RunStatus.PAUSED.name()), 100)) {
            if (RunLifecycleService.secondsSince(paused.getUpdatedAt())
                    > properties.getPauseTtlSeconds()) {
                log.warn("pause TTL expired, cancelling run={}", paused.getRunId());
                lifecycleService.forceTerminal(paused, RunStatus.CANCELLED,
                        "PAUSE_EXPIRED", "暂停超过保留时限，已自动取消；可重新发起分析");
            } else {
                permitService.renewLeases(paused.getConversationId(),
                        paused.getConvPermitId(), null);
            }
        }
        kickIfBacklog();
    }

    private void kickIfBacklog() {
        if (!draining
                && !queueService.listQueuedGlobal(1).isEmpty()
                && permitService.availableGlobalPermits() > 0) {
            kick();
        }
    }

    /**
     * Startup recovery: re-dispatch STARTING within grace; resume
     * RUNNING/WAITING_* that have a checkpoint; only mark ORPHANED when past
     * grace and neither the runtime nor a snapshot can continue the work.
     */
    void recoverAfterRestart() {
        List<AgentRun> actives = lifecycleService.listByStatuses(
                List.copyOf(RunStatus.ACTIVE), 500);
        int grace = properties.getStartGraceSeconds();
        for (AgentRun run : actives) {
            Optional<Map<String, Object>> live = runtimeClient.getRun(run.getRunId());
            boolean runtimeActive = live.isPresent()
                    && !RunStatus.isTerminal(String.valueOf(live.get().getOrDefault("status", "")));
            if (runtimeActive) {
                log.info("adopting live run after restart run={} status={}",
                        run.getRunId(), run.getStatus());
                continue;
            }
            if (RunStatus.PAUSING.name().equals(run.getStatus())
                    && run.getExecutionSnapshot() != null) {
                lifecycleService.settlePausedAfterRestart(run.getRunId());
                continue;
            }

            long ageSec = RunLifecycleService.secondsSince(run.getUpdatedAt());
            boolean withinGrace = ageSec <= grace;
            boolean hasCheckpoint = StringUtils.hasText(run.getExecutionSnapshot());

            if (RunStatus.STARTING.name().equals(run.getStatus()) && withinGrace) {
                log.info("re-dispatching STARTING run after restart run={} ageSec={}",
                        run.getRunId(), ageSec);
                String runId = run.getRunId();
                if (startPool != null) {
                    startPool.submit(() -> {
                        try {
                            lifecycleService.startRun(runId);
                        } catch (Exception e) {
                            log.warn("restart re-dispatch failed run={}: {}",
                                    runId, e.getMessage());
                        }
                    });
                } else {
                    try {
                        lifecycleService.startRun(runId);
                    } catch (Exception e) {
                        log.warn("restart re-dispatch failed run={}: {}",
                                runId, e.getMessage());
                    }
                }
                continue;
            }

            if (isResumableActive(run.getStatus()) && hasCheckpoint) {
                log.info("resuming checkpointed run after restart run={} status={}",
                        run.getRunId(), run.getStatus());
                try {
                    lifecycleService.resumeAfterRestart(run.getRunId());
                } catch (Exception e) {
                    log.warn("restart resume failed run={}: {}", run.getRunId(), e.getMessage());
                    if (!withinGrace) {
                        lifecycleService.forceTerminal(run, RunStatus.FAILED,
                                "ORPHANED_ON_RESTART",
                                "服务重启后恢复失败，已标记失败；可重新发起分析");
                    }
                }
                continue;
            }

            if (!withinGrace && !hasCheckpoint) {
                log.warn("orphaned run after restart run={} status={}, closing as FAILED",
                        run.getRunId(), run.getStatus());
                lifecycleService.forceTerminal(run, RunStatus.FAILED,
                        "ORPHANED_ON_RESTART",
                        "服务重启后运行状态无法恢复，已标记失败；可重新发起分析");
            } else {
                log.info("keeping run pending recovery run={} status={} ageSec={} checkpoint={}",
                        run.getRunId(), run.getStatus(), ageSec, hasCheckpoint);
            }
        }
        // PAUSED runs survive restarts by design: their snapshot lives in
        // MySQL and resume re-dispatches to any live runtime instance.
    }

    private static boolean isResumableActive(String status) {
        return RunStatus.RUNNING.name().equals(status)
                || RunStatus.WAITING_LLM.name().equals(status)
                || RunStatus.WAITING_TOOL.name().equals(status)
                || RunStatus.RESUMING.name().equals(status);
    }

    /** Snapshot used by safe-deploy drain wait loop. */
    public Map<String, Object> activeRunsSnapshot() {
        List<AgentRun> active = lifecycleService.listByStatuses(
                List.copyOf(RunStatus.ACTIVE), 500);
        List<Map<String, Object>> runs = new ArrayList<>();
        int withCheckpoint = 0;
        for (AgentRun run : active) {
            boolean checkpoint = StringUtils.hasText(run.getExecutionSnapshot());
            if (checkpoint) {
                withCheckpoint++;
            }
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("runId", run.getRunId());
            row.put("status", run.getStatus());
            row.put("hasCheckpoint", checkpoint);
            row.put("updatedAt", String.valueOf(run.getUpdatedAt()));
            runs.add(row);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("draining", draining);
        out.put("activeCount", active.size());
        out.put("checkpointedCount", withCheckpoint);
        out.put("readyToRestart", active.isEmpty()
                || withCheckpoint == active.size());
        out.put("runs", runs);
        return out;
    }

    private Thread daemon(Runnable r, String name) {
        Thread thread = new Thread(r, name);
        thread.setDaemon(true);
        return thread;
    }
}
