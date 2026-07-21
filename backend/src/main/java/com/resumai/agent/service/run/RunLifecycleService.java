package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.AgentRunProperties;
import com.resumai.agent.dao.AgentExecutionRecordMapper;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.ConversationMessageMapper;
import com.resumai.agent.dao.ConversationSessionMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.dao.ToolCallLogMapper;
import com.resumai.agent.domain.entity.AgentExecutionRecord;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationMessage;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.ToolCallLog;
import com.resumai.agent.domain.enums.RunStatus;
import com.resumai.agent.service.MemoryService;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Lazy;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Drives one run from STARTING to a terminal state: policy selection, runtime
 * start, event/status ingestion, cancellation propagation, permit release and
 * episodic memory write-back. Java owns the authoritative status; a late
 * runtime callback can never resurrect a terminal run.
 */
@Service
public class RunLifecycleService {

    private static final Logger log = LoggerFactory.getLogger(RunLifecycleService.class);

    private final AgentRunMapper runMapper;
    private final AgentExecutionRecordMapper executionMapper;
    private final ToolCallLogMapper toolCallMapper;
    private final ConversationSessionMapper sessionMapper;
    private final ConversationMessageMapper messageMapper;
    private final ResumeTaskMapper resumeTaskMapper;
    private final RunEventService eventService;
    private final RunPermitService permitService;
    private final PolicyService policyService;
    private final RewardService rewardService;
    private final AgentRuntimeClient runtimeClient;
    private final MemoryService memoryService;
    private final AgentRunProperties properties;
    private final ObjectMapper objectMapper;
    private final org.springframework.context.ApplicationEventPublisher eventPublisher;
    private final org.redisson.api.RedissonClient redisson;

    public RunLifecycleService(AgentRunMapper runMapper,
                               AgentExecutionRecordMapper executionMapper,
                               ToolCallLogMapper toolCallMapper,
                               ConversationSessionMapper sessionMapper,
                               ConversationMessageMapper messageMapper,
                               ResumeTaskMapper resumeTaskMapper,
                               RunEventService eventService,
                               RunPermitService permitService,
                               PolicyService policyService,
                               RewardService rewardService,
                               AgentRuntimeClient runtimeClient,
                               @Lazy MemoryService memoryService,
                               AgentRunProperties properties,
                               ObjectMapper objectMapper,
                               org.springframework.context.ApplicationEventPublisher eventPublisher,
                               org.redisson.api.RedissonClient redisson) {
        this.runMapper = runMapper;
        this.executionMapper = executionMapper;
        this.toolCallMapper = toolCallMapper;
        this.sessionMapper = sessionMapper;
        this.messageMapper = messageMapper;
        this.resumeTaskMapper = resumeTaskMapper;
        this.eventService = eventService;
        this.permitService = permitService;
        this.policyService = policyService;
        this.rewardService = rewardService;
        this.runtimeClient = runtimeClient;
        this.memoryService = memoryService;
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.eventPublisher = eventPublisher;
        this.redisson = redisson;
    }

    /** Called by the scheduler once permits are held and status is STARTING. */
    public void startRun(String runId) {
        AgentRun run = runMapper.selectById(runId);
        if (run == null || !RunStatus.STARTING.name().equals(run.getStatus())) {
            return;
        }
        ConversationSession session = sessionMapper.selectById(run.getConversationId());
        if (session == null) {
            finishInternal(run, RunStatus.FAILED, null, "CONVERSATION_MISSING", "会话不存在", null);
            return;
        }
        try {
            Map<String, Object> selectionContext = selectionContext(run, session);
            PolicyService.Selection selection = StringUtils.hasText(run.getPolicyId())
                    ? policyService.forcedSelection(runId, category(run), run.getPolicyId())
                    : policyService.selectPolicy(runId, category(run), selectionContext);
            PolicyBundleRow bundle = selection.bundle();
            JsonNode config = readJson(bundle.getConfig());
            int runTimeout = config.path("timeoutPolicy").path("runTimeoutSeconds")
                    .asInt(properties.getRunTimeoutSeconds());

            LocalDateTime now = LocalDateTime.now();
            UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
            update.eq("run_id", runId)
                    .eq("status", RunStatus.STARTING.name())
                    .set("policy_id", bundle.getPolicyId())
                    .set("status", RunStatus.RUNNING.name())
                    .set("started_at", now)
                    .set("timeout_at", now.plusSeconds(runTimeout))
                    .set("updated_at", now);
            if (runMapper.update(null, update) == 0) {
                // Cancelled while starting; permits are released by cancel path.
                return;
            }
            eventService.publish(runId, run.getConversationId(), run.getTraceId(),
                    "run.started", null, null, Map.of(
                            "policyId", bundle.getPolicyId(),
                            "policyName", bundle.getName() != null ? bundle.getName() : "",
                            "selectionMode", selection.mode(),
                            "runType", category(run)));
            Map<String, Object> payload = buildRuntimePayload(run, session, bundle);
            if ("FORCED".equals(selection.mode())) {
                // Benchmark/replay runs execute their deterministic tools in
                // isolated Docker workers; normal user requests stay in-process.
                payload.put("isolatedSandbox", true);
            }
            // Checkpoint retry: a queued run created from a failed run carries
            // its snapshot — the runtime resumes after the last finished group.
            if (StringUtils.hasText(run.getExecutionSnapshot())) {
                Map<String, Object> checkpoint = readJsonAsMap(run.getExecutionSnapshot());
                if (checkpoint != null && !checkpoint.isEmpty()) {
                    payload.put("resumeSnapshot", checkpoint);
                }
            }
            runtimeClient.startRun(payload);
        } catch (Exception e) {
            log.warn("run start failed run={}: {}", runId, e.getMessage());
            AgentRun latest = runMapper.selectById(runId);
            if (latest != null && !RunStatus.isTerminal(latest.getStatus())) {
                finishInternal(latest, RunStatus.FAILED, null,
                        "RUNTIME_START_FAILED", trim(e.getMessage(), 1800), null);
            }
        }
    }

    private Map<String, Object> selectionContext(AgentRun run, ConversationSession session) {
        Map<String, Object> context = new LinkedHashMap<>();
        String resume = session.getResumeText() != null ? session.getResumeText() : "";
        String jd = session.getJobDescription() != null ? session.getJobDescription() : "";
        context.put("goal", trim(run.getUserMessage(), 300));
        context.put("jobCategory", session.getJobCategory());
        context.put("resumeLength", resume.length());
        context.put("resumePages", Math.max(1, resume.length() / 2600));
        context.put("projectCount", countOccurrences(resume, "项目"));
        context.put("workExperienceCount", countOccurrences(resume, "公司"));
        context.put("jdRequirementCount", countOccurrences(jd, "\n"));
        context.put("conversationRevision", session.getActiveRevision());
        return context;
    }

    private Map<String, Object> buildRuntimePayload(AgentRun run, ConversationSession session,
                                                    PolicyBundleRow bundle) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("runId", run.getRunId());
        payload.put("conversationId", run.getConversationId());
        payload.put("userId", run.getUserId());
        payload.put("traceId", run.getTraceId());
        payload.put("revision", run.getRevisionNo());
        payload.put("runType", category(run));
        payload.put("userMessage", run.getUserMessage());
        payload.put("planMode", run.getPlanMode() != null && run.getPlanMode() == 1);
        payload.put("resumeText", session.getResumeText());
        payload.put("jobDescription", session.getJobDescription());
        payload.put("jobCategory", session.getJobCategory());
        payload.put("conversationSummary", session.getSummary());
        payload.put("currentGoal", session.getCurrentGoal());
        payload.put("policyId", bundle.getPolicyId());
        payload.put("policyConfig", readJsonAsMap(bundle.getConfig()));
        payload.put("recentMessages", recentMessages(run.getConversationId(), 12));
        if (StringUtils.hasText(run.getSourceTaskTraceId())) {
            payload.put("sourceTaskTraceId", run.getSourceTaskTraceId());
        }
        return payload;
    }

    private List<Map<String, Object>> recentMessages(String conversationId, int limit) {
        List<ConversationMessage> rows = messageMapper.selectList(
                new QueryWrapper<ConversationMessage>()
                        .eq("conversation_id", conversationId)
                        .orderByDesc("id")
                        .last("limit " + limit));
        List<Map<String, Object>> out = new ArrayList<>();
        for (int i = rows.size() - 1; i >= 0; i--) {
            ConversationMessage message = rows.get(i);
            Map<String, Object> view = new LinkedHashMap<>();
            view.put("id", message.getId());
            view.put("role", message.getRole());
            view.put("content", message.getContent());
            view.put("intent", message.getIntentType());
            out.add(view);
        }
        return out;
    }

    // ------------------------------------------------------------------
    // Runtime event/result ingestion
    // ------------------------------------------------------------------

    private static final Map<String, String> PHASE_STATUS = Map.of(
            "llm.started", RunStatus.WAITING_LLM.name(),
            "llm.completed", RunStatus.RUNNING.name(),
            "llm.failed", RunStatus.RUNNING.name(),
            "tool.started", RunStatus.WAITING_TOOL.name(),
            "tool.completed", RunStatus.RUNNING.name(),
            "tool.failed", RunStatus.RUNNING.name(),
            "sandbox.started", RunStatus.WAITING_SANDBOX.name(),
            "sandbox.completed", RunStatus.RUNNING.name(),
            "sandbox.failed", RunStatus.RUNNING.name());

    public void applyRuntimeEvent(String runId, String eventType, String agentId,
                                  String toolName, Map<String, Object> payload) {
        AgentRun run = runMapper.selectById(runId);
        if (run == null) {
            log.info("event for unknown run dropped run={} type={}", runId, eventType);
            return;
        }
        if (RunStatus.isTerminal(run.getStatus())) {
            return; // late event after cancel/timeout — keep for audit only
        }
        eventService.publish(runId, run.getConversationId(), run.getTraceId(),
                eventType, agentId, toolName, payload);
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .in("status", RunStatus.ACTIVE)
                .ne("status", RunStatus.CANCELLING.name())
                .set("updated_at", LocalDateTime.now());
        boolean dirty = false;
        if (StringUtils.hasText(agentId)) {
            update.set("current_agent", agentId);
            dirty = true;
        }
        if (StringUtils.hasText(toolName)) {
            update.set("current_tool", toolName);
            dirty = true;
        }
        String phaseStatus = PHASE_STATUS.get(eventType);
        if (phaseStatus != null) {
            update.set("status", phaseStatus);
            update.set("current_phase", eventType);
            dirty = true;
        }
        if ("agent.started".equals(eventType) || "agent.completed".equals(eventType)) {
            update.set("current_phase", eventType);
            dirty = true;
        }
        if (dirty) {
            runMapper.update(null, update);
        }
        recordStructuredEvent(run, eventType, agentId, toolName, payload);
    }

    private void recordStructuredEvent(AgentRun run, String eventType, String agentId,
                                       String toolName, Map<String, Object> payload) {
        try {
            if ("agent.started".equals(eventType)) {
                AgentExecutionRecord record = new AgentExecutionRecord();
                record.setRunId(run.getRunId());
                record.setAgentId(agentId);
                record.setStatus("RUNNING");
                record.setIterations(0);
                record.setLlmCalls(0);
                record.setToolCalls(0);
                record.setStartedAt(LocalDateTime.now());
                record.setCreateTime(LocalDateTime.now());
                executionMapper.insert(record);
            } else if ("agent.completed".equals(eventType) || "agent.failed".equals(eventType)) {
                AgentExecutionRecord record = executionMapper.selectOne(
                        new QueryWrapper<AgentExecutionRecord>()
                                .eq("run_id", run.getRunId())
                                .eq("agent_id", agentId)
                                .orderByDesc("id")
                                .last("limit 1"));
                if (record != null) {
                    record.setStatus("agent.completed".equals(eventType) ? "SUCCEEDED" : "FAILED");
                    record.setIterations(intOf(payload.get("iterations")));
                    record.setLlmCalls(intOf(payload.get("llmCalls")));
                    record.setToolCalls(intOf(payload.get("toolCalls")));
                    record.setOutput(writeJson(payload.get("output")));
                    record.setErrorMessage(trim(stringOf(payload.get("error")), 1800));
                    record.setFinishedAt(LocalDateTime.now());
                    executionMapper.updateById(record);
                }
            } else if ("tool.started".equals(eventType) && payload.get("toolCallId") != null) {
                ToolCallLog call = new ToolCallLog();
                call.setToolCallId(stringOf(payload.get("toolCallId")));
                call.setRunId(run.getRunId());
                call.setAgentId(agentId);
                call.setToolName(toolName);
                call.setArguments(writeJson(payload.get("arguments")));
                call.setStatus("RUNNING");
                call.setRetryCount(intOf(payload.get("retryCount")));
                call.setIdempotencyKey(stringOf(payload.get("idempotencyKey")));
                call.setSideEffectLevel(stringOf(payload.get("sideEffectLevel")));
                call.setStartedAt(LocalDateTime.now());
                call.setHeartbeatAt(LocalDateTime.now());
                toolCallMapper.insert(call);
            } else if (("tool.completed".equals(eventType) || "tool.failed".equals(eventType))
                    && payload.get("toolCallId") != null) {
                ToolCallLog call = toolCallMapper.selectById(stringOf(payload.get("toolCallId")));
                if (call != null) {
                    call.setStatus("tool.completed".equals(eventType) ? "SUCCEEDED" : "FAILED");
                    call.setResultPreview(trim(stringOf(payload.get("resultPreview")), 3900));
                    call.setError(trim(stringOf(payload.get("error")), 1800));
                    call.setDurationMs(longOf(payload.get("durationMs")));
                    call.setRetryCount(intOf(payload.get("retryCount")));
                    call.setFinishedAt(LocalDateTime.now());
                    toolCallMapper.updateById(call);
                }
            } else if ("tool.progress".equals(eventType) && payload.get("toolCallId") != null) {
                UpdateWrapper<ToolCallLog> update = new UpdateWrapper<>();
                update.eq("tool_call_id", stringOf(payload.get("toolCallId")))
                        .set("progress", trim(stringOf(payload.get("progress")), 250))
                        .set("heartbeat_at", LocalDateTime.now());
                toolCallMapper.update(null, update);
            }
        } catch (Exception e) {
            log.debug("structured event record failed run={} type={}: {}",
                    run.getRunId(), eventType, e.getMessage());
        }
    }

    public record RuntimeResult(String status, String answer, String errorCode, String errorMessage,
                                Map<String, Object> sharedState, Map<String, Object> metrics,
                                Map<String, Object> promptVersions, Map<String, Object> skillVersions,
                                String conversationSummary, String currentGoal,
                                Map<String, Object> executionSnapshot,
                                Map<String, Object> structuredReport) {
    }

    /** Final callback from the runtime. Returns true when accepted. */
    public boolean applyRuntimeResult(String runId, RuntimeResult result) {
        AgentRun run = runMapper.selectById(runId);
        if (run == null) {
            return false;
        }
        if (RunStatus.isTerminal(run.getStatus())) {
            log.info("late runtime result fenced run={} incoming={} current={}",
                    runId, result.status(), run.getStatus());
            return false;
        }
        String incoming = result.status() != null ? result.status() : "FAILED";
        if ("PAUSED".equals(incoming)) {
            return applyPausedResult(run, result);
        }
        RunStatus terminal = switch (incoming) {
            case "SUCCEEDED", "SUCCESS" -> RunStatus.SUCCEEDED;
            case "PARTIAL_SUCCESS" -> RunStatus.PARTIAL_SUCCESS;
            case "CANCELLED" -> RunStatus.CANCELLED;
            case "TIMED_OUT" -> RunStatus.TIMED_OUT;
            default -> RunStatus.FAILED;
        };
        if (RunStatus.CANCELLING.name().equals(run.getStatus())
                && (terminal == RunStatus.SUCCEEDED || terminal == RunStatus.PARTIAL_SUCCESS)) {
            // The user asked to stop; a completion racing past the cancel does
            // not overturn it. Keep the answer for audit but finish CANCELLED.
            terminal = RunStatus.CANCELLED;
        }
        finishInternal(run, terminal, result, result.errorCode(), result.errorMessage(), null);
        return true;
    }

    /** Runtime reached a safe boundary and delivered the execution snapshot. */
    private boolean applyPausedResult(AgentRun run, RuntimeResult result) {
        if (!RunStatus.PAUSING.name().equals(run.getStatus())
                && !RunStatus.isActive(run.getStatus())) {
            log.info("paused result fenced run={} current={}", run.getRunId(), run.getStatus());
            return false;
        }
        LocalDateTime now = LocalDateTime.now();
        Map<String, Object> snapshot = result.executionSnapshot();
        boolean awaitingPlanApproval = snapshot != null
                && "AWAITING_PLAN_APPROVAL".equals(snapshot.get("pauseReason"));
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", run.getRunId())
                .in("status", RunStatus.ACTIVE)
                .set("status", RunStatus.PAUSED.name())
                .set("execution_snapshot", writeJson(snapshot))
                .set("updated_at", now)
                .set("current_phase", null);
        if (awaitingPlanApproval) {
            update.set("pause_reason", "AWAITING_PLAN_APPROVAL");
        }
        if (runMapper.update(null, update) == 0) {
            return false;
        }
        // Free the global slot (another conversation may run); keep the
        // conversation permit so this conversation stays strictly serial.
        AgentRun paused = runMapper.selectById(run.getRunId());
        permitService.releaseGlobal(paused.getGlobalPermitId());
        UpdateWrapper<AgentRun> clearGlobal = new UpdateWrapper<>();
        clearGlobal.eq("run_id", run.getRunId()).set("global_permit_id", null);
        runMapper.update(null, clearGlobal);
        eventService.publish(run.getRunId(), run.getConversationId(), run.getTraceId(),
                "run.progress", null, null, Map.of(
                        "stage", awaitingPlanApproval ? "awaiting_plan_approval" : "paused",
                        "message", awaitingPlanApproval
                                ? "Coordinator 已产出执行计划，等待确认后开始评估"
                                : "已在安全边界暂停，可随时恢复",
                        "plan", snapshot != null
                                ? snapshot.getOrDefault("plan", List.of()) : List.of()));
        // Mirror the awaiting state onto the linked task so the UI can show
        // the plan-approval card instead of a generic PAUSED badge.
        if (awaitingPlanApproval && StringUtils.hasText(paused.getSourceTaskTraceId())) {
            UpdateWrapper<com.resumai.agent.domain.entity.ResumeTask> taskUpdate = new UpdateWrapper<>();
            taskUpdate.eq("trace_id", paused.getSourceTaskTraceId())
                    .notIn("status", "SUCCESS", "PARTIAL_SUCCESS", "CANCELLED", "SUPERSEDED")
                    .set("status", "PAUSED")
                    .set("queue_status", "PAUSED")
                    .set("update_time", now);
            resumeTaskMapper.update(null, taskUpdate);
            eventPublisher.publishEvent(new TaskRunSyncedEvent(
                    paused.getSourceTaskTraceId(), "PAUSED"));
        }
        log.info("run paused at safe boundary run={} planApproval={}",
                run.getRunId(), awaitingPlanApproval);
        return true;
    }

    /** User-initiated pause of an active run: CAS to PAUSING then propagate. */
    public AgentRun pauseActiveRun(AgentRun run, String reason) {
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", run.getRunId())
                .in("status", RunStatus.ACTIVE)
                .notIn("status", RunStatus.CANCELLING.name(), RunStatus.PAUSING.name())
                .set("status", RunStatus.PAUSING.name())
                .set("pause_reason", trim(reason, 480))
                .set("updated_at", LocalDateTime.now());
        runMapper.update(null, update);
        AgentRun latest = runMapper.selectById(run.getRunId());
        if (RunStatus.PAUSING.name().equals(latest.getStatus())) {
            try {
                runtimeClient.pauseRun(latest.getRunId(),
                        reason != null ? reason : "user_paused");
            } catch (Exception e) {
                log.warn("pause propagation failed run={}: {}", latest.getRunId(), e.getMessage());
                // revert: the runtime never saw the pause request
                UpdateWrapper<AgentRun> revert = new UpdateWrapper<>();
                revert.eq("run_id", latest.getRunId())
                        .eq("status", RunStatus.PAUSING.name())
                        .set("status", RunStatus.RUNNING.name())
                        .set("updated_at", LocalDateTime.now());
                runMapper.update(null, revert);
                return runMapper.selectById(run.getRunId());
            }
        }
        return latest;
    }

    /** Resume a PAUSED run using its stored execution snapshot. */
    public AgentRun resumePausedRun(AgentRun run) {
        return resumePausedRun(run, null);
    }

    /**
     * Resume with an optional user-approved plan (plan-approval mode): the
     * edited plan replaces the snapshot's plan and grouping/budget are
     * recomputed by the runtime from dependency rules.
     */
    public AgentRun resumePausedRun(AgentRun run, List<String> approvedPlan) {
        if (!RunStatus.PAUSED.name().equals(run.getStatus())) {
            return run;
        }
        if (approvedPlan != null && !approvedPlan.isEmpty()) {
            Map<String, Object> snapshot = readJsonAsMap(run.getExecutionSnapshot());
            if (snapshot != null && !snapshot.isEmpty()) {
                snapshot.put("plan", approvedPlan);
                snapshot.remove("parallelGroups");
                snapshot.remove("budgetPlan");
                snapshot.put("nextPlanIndex", 0);
                UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
                update.eq("run_id", run.getRunId())
                        .eq("status", RunStatus.PAUSED.name())
                        .set("execution_snapshot", writeJson(snapshot))
                        .set("updated_at", LocalDateTime.now());
                runMapper.update(null, update);
                run = runMapper.selectById(run.getRunId());
            }
        }
        String globalPermit = permitService.tryAcquireGlobal();
        if (globalPermit == null) {
            throw new IllegalStateException("全局并发已满，稍后再恢复");
        }
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", run.getRunId())
                .eq("status", RunStatus.PAUSED.name())
                .set("status", RunStatus.RESUMING.name())
                .set("global_permit_id", globalPermit)
                .set("updated_at", LocalDateTime.now());
        if (runMapper.update(null, update) == 0) {
            permitService.releaseGlobal(globalPermit);
            return runMapper.selectById(run.getRunId());
        }
        AgentRun latest = runMapper.selectById(run.getRunId());
        try {
            ConversationSession session = sessionMapper.selectById(latest.getConversationId());
            PolicyBundleRow bundle = policyService.getBundle(latest.getPolicyId());
            Map<String, Object> payload = buildRuntimePayload(latest, session, bundle);
            payload.put("resumeSnapshot", readJsonAsMap(latest.getExecutionSnapshot()));
            runtimeClient.resumeRun(latest.getRunId(), payload);
            UpdateWrapper<AgentRun> toRunning = new UpdateWrapper<>();
            toRunning.eq("run_id", latest.getRunId())
                    .eq("status", RunStatus.RESUMING.name())
                    .set("status", RunStatus.RUNNING.name())
                    .set("updated_at", LocalDateTime.now());
            runMapper.update(null, toRunning);
            eventService.publish(latest.getRunId(), latest.getConversationId(), latest.getTraceId(),
                    "run.progress", null, null, Map.of(
                            "stage", "resumed", "message", "已从暂停快照恢复执行"));
        } catch (Exception e) {
            log.warn("resume propagation failed run={}: {}", latest.getRunId(), e.getMessage());
            permitService.releaseGlobal(globalPermit);
            UpdateWrapper<AgentRun> revert = new UpdateWrapper<>();
            revert.eq("run_id", latest.getRunId())
                    .eq("status", RunStatus.RESUMING.name())
                    .set("status", RunStatus.PAUSED.name())
                    .set("global_permit_id", null)
                    .set("updated_at", LocalDateTime.now());
            runMapper.update(null, revert);
            throw new IllegalStateException("恢复运行失败：" + e.getMessage(), e);
        }
        return runMapper.selectById(run.getRunId());
    }

    // ------------------------------------------------------------------
    // Cancellation, timeout, forced termination
    // ------------------------------------------------------------------

    /** User-initiated stop of an active run: CAS to CANCELLING then propagate. */
    public AgentRun cancelActiveRun(AgentRun run, String reasonCode, String reasonText) {
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", run.getRunId())
                .in("status", RunStatus.ACTIVE)
                .ne("status", RunStatus.CANCELLING.name())
                .set("status", RunStatus.CANCELLING.name())
                .set("cancellation_reason", reasonText)
                .set("updated_at", LocalDateTime.now());
        runMapper.update(null, update);
        AgentRun latest = runMapper.selectById(run.getRunId());
        if (RunStatus.CANCELLING.name().equals(latest.getStatus())) {
            eventService.publish(latest.getRunId(), latest.getConversationId(), latest.getTraceId(),
                    "run.cancelling", null, null, Map.of("reason", reasonText != null ? reasonText : ""));
            runtimeClient.cancelRun(latest.getRunId(),
                    reasonCode != null ? reasonCode : "user_cancelled");
        }
        return latest;
    }

    /** Force a terminal state (watchdog: timeout, orphan, stuck cancel). */
    public void forceTerminal(AgentRun run, RunStatus terminal, String errorCode, String message) {
        if (terminal == RunStatus.TIMED_OUT || terminal == RunStatus.CANCELLED) {
            runtimeClient.cancelRun(run.getRunId(), errorCode != null ? errorCode : terminal.name());
        }
        AgentRun latest = runMapper.selectById(run.getRunId());
        if (latest == null || RunStatus.isTerminal(latest.getStatus())) {
            return;
        }
        finishInternal(latest, terminal, null, errorCode, message, null);
    }

    private void finishInternal(AgentRun run, RunStatus terminal, RuntimeResult result,
                                String errorCode, String errorMessage, String answerOverride) {
        LocalDateTime now = LocalDateTime.now();
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", run.getRunId())
                .notIn("status", RunStatus.TERMINAL)
                .set("status", terminal.name())
                .set("finished_at", now)
                .set("updated_at", now)
                .set("current_phase", null);
        if (errorCode != null) {
            update.set("error_code", errorCode);
        }
        if (errorMessage != null) {
            update.set("error_message", trim(errorMessage, 1900));
        }
        if (result != null) {
            if (result.answer() != null) {
                update.set("answer", result.answer());
            }
            if (result.sharedState() != null) {
                update.set("shared_state", writeJson(result.sharedState()));
            }
            if (result.metrics() != null) {
                update.set("metrics", writeJson(result.metrics()));
            }
            if (result.promptVersions() != null) {
                update.set("prompt_versions", writeJson(result.promptVersions()));
            }
            if (result.skillVersions() != null) {
                update.set("skill_versions", writeJson(result.skillVersions()));
            }
        }
        if (answerOverride != null) {
            update.set("answer", answerOverride);
        }
        if (runMapper.update(null, update) == 0) {
            return; // another finisher won
        }
        AgentRun finished = runMapper.selectById(run.getRunId());

        // 1. release distributed permits so the next queued run can start
        permitService.releaseConversation(finished.getConversationId(), finished.getConvPermitId());
        permitService.releaseGlobal(finished.getGlobalPermitId());
        clearPermits(finished.getRunId());

        // 2. persist assistant answer as a conversation message
        boolean answered = (terminal == RunStatus.SUCCEEDED
                || terminal == RunStatus.PARTIAL_SUCCESS)
                && result != null && StringUtils.hasText(result.answer());
        if (answered) {
            saveAssistantMessage(finished, result.answer());
            updateSessionAfterRun(finished, result);
        }

        // 2b. mirror the outcome onto the originating resume_task (if any)
        syncSourceTask(finished, terminal, result, errorMessage);

        // 3. emit terminal SSE event
        String eventType = switch (terminal) {
            case SUCCEEDED, PARTIAL_SUCCESS -> "run.completed";
            case CANCELLED -> "run.cancelled";
            case TIMED_OUT -> "run.timed_out";
            default -> "run.failed";
        };
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("status", terminal.name());
        payload.put("errorCode", errorCode != null ? errorCode : "");
        payload.put("errorMessage", errorMessage != null ? trim(errorMessage, 500) : "");
        if (result != null && StringUtils.hasText(result.answer())) {
            payload.put("answer", result.answer());
        }
        eventService.publish(finished.getRunId(), finished.getConversationId(), finished.getTraceId(),
                eventType, null, null, payload);

        // 4. learning signals + episodic memory
        try {
            boolean succeeded = terminal == RunStatus.SUCCEEDED
                    || terminal == RunStatus.PARTIAL_SUCCESS;
            if (StringUtils.hasText(finished.getPolicyId())) {
                policyService.recordRunOutcome(finished.getPolicyId(), category(finished),
                        succeeded);
                rewardService.recordAutoReward(finished, terminal == RunStatus.SUCCEEDED);
            }
            memoryService.writeEpisodicRunMemory(finished, terminal.name());
            if (terminal == RunStatus.FAILED || terminal == RunStatus.TIMED_OUT) {
                memoryService.writeFailureMemory(finished, errorCode, errorMessage);
            }
        } catch (Exception e) {
            log.debug("post-run learning hooks failed run={}: {}", finished.getRunId(), e.getMessage());
        }
        log.info("run finished run={} status={} conversation={}",
                finished.getRunId(), terminal, finished.getConversationId());
    }

    /**
     * Bridge to the legacy resume_task table: a run created for an uploaded
     * resume evaluation mirrors its terminal state, answer and — when the
     * ReportAgent produced one — the validated structured report (score,
     * recommendation, dimensions) back so the task list, score circle and
     * report tab stay truthful. No score is ever fabricated.
     */
    private void syncSourceTask(AgentRun run, RunStatus terminal, RuntimeResult result,
                                String errorMessage) {
        if (!StringUtils.hasText(run.getSourceTaskTraceId())) {
            return;
        }
        try {
            String taskStatus = switch (terminal) {
                case SUCCEEDED -> "SUCCESS";
                case PARTIAL_SUCCESS -> "PARTIAL_SUCCESS";
                case CANCELLED -> "CANCELLED";
                default -> "FAILED";
            };
            String answer = result != null && StringUtils.hasText(result.answer())
                    ? result.answer() : null;
            Long durationMs = null;
            Integer tokenCost = null;
            if (result != null && result.metrics() != null) {
                Object latency = result.metrics().get("latencySeconds");
                if (latency instanceof Number n) {
                    durationMs = (long) (n.doubleValue() * 1000);
                }
                Object prompt = result.metrics().get("promptTokens");
                Object completion = result.metrics().get("completionTokens");
                if (prompt instanceof Number p && completion instanceof Number c) {
                    tokenCost = p.intValue() + c.intValue();
                }
            }
            Map<String, Object> report = result != null && result.structuredReport() != null
                    ? result.structuredReport() : Map.of();
            Integer overallScore = report.get("overallScore") instanceof Number n
                    ? n.intValue() : null;
            String recommendation = report.get("recommendation") instanceof String s
                    && StringUtils.hasText(s) ? s : null;

            UpdateWrapper<com.resumai.agent.domain.entity.ResumeTask> update = new UpdateWrapper<>();
            update.eq("trace_id", run.getSourceTaskTraceId())
                    .notIn("status", "SUCCESS", "PARTIAL_SUCCESS", "CANCELLED", "SUPERSEDED")
                    .set("status", taskStatus)
                    .set("queue_status", taskStatus)
                    .set("finished_at", LocalDateTime.now())
                    .set("update_time", LocalDateTime.now());
            if (answer != null) {
                update.set("summary", trim(reportSummaryLine(report, answer), 1900));
            } else if (StringUtils.hasText(errorMessage)) {
                update.set("summary", trim(errorMessage, 1900));
            }
            if (durationMs != null) {
                update.set("duration_ms", durationMs);
            }
            if (tokenCost != null) {
                update.set("token_cost", tokenCost);
            }
            if (overallScore != null) {
                update.set("overall_score", overallScore);
            }
            if (recommendation != null) {
                update.set("recommendation", recommendation);
            }
            String payload = buildTaskResultPayload(run, taskStatus, answer, report,
                    durationMs, tokenCost);
            if (payload != null) {
                update.set("result_payload", payload);
            }
            resumeTaskMapper.update(null, update);
            // The task list, dashboard counters and /api/tasks/{id} serve from
            // the in-memory task cache — refresh it so the UI flips immediately.
            eventPublisher.publishEvent(new TaskRunSyncedEvent(
                    run.getSourceTaskTraceId(), taskStatus));
        } catch (Exception e) {
            log.warn("resume_task sync failed run={} task={}: {}",
                    run.getRunId(), run.getSourceTaskTraceId(), e.getMessage());
        }
    }

    /** First strengths/risks line as the one-line list summary, else the answer head. */
    private String reportSummaryLine(Map<String, Object> report, String answer) {
        Object recommendation = report.get("recommendation");
        Object score = report.get("overallScore");
        if (recommendation != null || score != null) {
            StringBuilder line = new StringBuilder();
            if (score != null) {
                line.append("综合评分 ").append(score).append(" 分");
            }
            if (recommendation != null) {
                if (line.length() > 0) {
                    line.append(" · ");
                }
                line.append(recommendationLabel(String.valueOf(recommendation)));
            }
            String head = answer.replaceAll("^#+\\s*", "").replaceAll("\\s+", " ");
            line.append("。").append(head, 0, Math.min(head.length(), 220));
            return line.toString();
        }
        return answer;
    }

    private static String recommendationLabel(String recommendation) {
        return switch (recommendation) {
            case "HIRE" -> "建议录用";
            case "INTERVIEW_RECOMMEND" -> "推荐面试";
            case "NOT_RECOMMEND" -> "不推荐";
            default -> "需人工复核";
        };
    }

    /**
     * result_payload JSON compatible with ResumeEvaluationService.hydrateTaskFromPayload —
     * this is what makes the task survive restarts with score/report intact.
     */
    private String buildTaskResultPayload(AgentRun run, String taskStatus, String answer,
                                          Map<String, Object> report,
                                          Long durationMs, Integer tokenCost) {
        try {
            com.resumai.agent.domain.entity.ResumeTask row = resumeTaskMapper.selectOne(
                    new QueryWrapper<com.resumai.agent.domain.entity.ResumeTask>()
                            .eq("trace_id", run.getSourceTaskTraceId()).last("limit 1"));
            if (row == null) {
                return null;
            }
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("fileName", StringUtils.hasText(row.getFileName())
                    ? row.getFileName() : row.getCandidateName());
            payload.put("jobCategory", row.getJobCategory());
            payload.put("executionMode", row.getExecutionMode());
            payload.put("status", taskStatus);
            if (report.get("overallScore") instanceof Number score) {
                payload.put("overallScore", score.intValue());
            }
            if (report.get("recommendation") instanceof String recommendation) {
                payload.put("recommendation", recommendation);
            }
            payload.put("summary", trim(reportSummaryLine(report,
                    answer != null ? answer : ""), 1900));
            payload.put("fullReport", answer != null ? answer : "");
            payload.put("structuredReport", report);
            payload.put("strengths", report.getOrDefault("strengths", List.of()));
            payload.put("risks", report.getOrDefault("risks", List.of()));
            payload.put("interviewQuestions", report.getOrDefault("interviewQuestions", List.of()));
            if (durationMs != null) {
                payload.put("durationMs", durationMs);
            }
            if (tokenCost != null) {
                payload.put("tokenCost", tokenCost);
            }
            payload.put("jobDescription", row.getJobDescription());
            payload.put("matchedJdTitle", row.getMatchedJdTitle());
            if (row.getJdMatchScore() != null) {
                payload.put("jdMatchScore", row.getJdMatchScore());
            }
            return objectMapper.writeValueAsString(payload);
        } catch (Exception e) {
            log.debug("build result payload failed run={}: {}", run.getRunId(), e.getMessage());
            return null;
        }
    }

    /** Published after a linked resume_task row was mirrored from a run. */
    public record TaskRunSyncedEvent(String traceId, String status) {
    }

    private void clearPermits(String runId) {
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .set("conv_permit_id", null)
                .set("global_permit_id", null);
        runMapper.update(null, update);
    }

    private void saveAssistantMessage(AgentRun run, String answer) {
        try {
            ConversationMessage message = new ConversationMessage();
            message.setConversationId(run.getConversationId());
            message.setClientMessageId(run.getRunId() + ":assistant");
            message.setRole("ASSISTANT");
            message.setIntentType(run.getRunType());
            message.setContent(answer);
            message.setRevisionNo(run.getRevisionNo());
            message.setRunId(run.getRunId());
            message.setCreateTime(LocalDateTime.now());
            message.setDeleted(0);
            messageMapper.insert(message);
        } catch (org.springframework.dao.DuplicateKeyException duplicate) {
            log.debug("assistant message already stored run={}", run.getRunId());
        }
    }

    private void updateSessionAfterRun(AgentRun run, RuntimeResult result) {
        ConversationSession session = sessionMapper.selectById(run.getConversationId());
        if (session == null) {
            return;
        }
        boolean dirty = false;
        if (StringUtils.hasText(result.conversationSummary())) {
            session.setSummary(result.conversationSummary());
            session.setSummaryVersion(
                    (session.getSummaryVersion() != null ? session.getSummaryVersion() : 0) + 1);
            dirty = true;
        }
        if (StringUtils.hasText(result.currentGoal())) {
            session.setCurrentGoal(result.currentGoal());
            dirty = true;
        }
        if (dirty) {
            session.setUpdateTime(LocalDateTime.now());
            sessionMapper.updateById(session);
            // Keep the Redis hot-session cache coherent (write path lives in
            // ConversationService; here we just invalidate).
            try {
                redisson.getMapCache("resumai:conversation:hot").fastRemove(session.getId());
            } catch (Exception e) {
                log.debug("session cache evict skipped {}: {}", session.getId(), e.getMessage());
            }
        }
    }

    /** Persist a group-boundary checkpoint from the runtime (active runs only). */
    public boolean saveRunCheckpoint(String runId, Map<String, Object> snapshot) {
        if (snapshot == null || snapshot.isEmpty()) {
            return false;
        }
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .notIn("status", RunStatus.TERMINAL)
                .set("execution_snapshot", writeJson(snapshot))
                .set("updated_at", LocalDateTime.now());
        return runMapper.update(null, update) > 0;
    }

    /**
     * Retry a FAILED/TIMED_OUT run from its last group-boundary checkpoint:
     * a fresh QUEUED run (same conversation/trace/revision) carrying the old
     * execution snapshot — completed agents and tool calls never re-execute.
     */
    public AgentRun retryFromCheckpoint(String failedRunId) {
        AgentRun failed = runMapper.selectById(failedRunId);
        if (failed == null) {
            throw new IllegalArgumentException("run 不存在: " + failedRunId);
        }
        if (!RunStatus.FAILED.name().equals(failed.getStatus())
                && !RunStatus.TIMED_OUT.name().equals(failed.getStatus())) {
            throw new IllegalStateException("仅 FAILED/TIMED_OUT 的 run 支持断点重试，当前: "
                    + failed.getStatus());
        }
        AgentRun retry = new AgentRun();
        retry.setRunId("run-" + java.util.UUID.randomUUID());
        retry.setConversationId(failed.getConversationId());
        retry.setUserId(failed.getUserId());
        retry.setTraceId(failed.getTraceId());
        retry.setRevisionNo(failed.getRevisionNo());
        retry.setRunType(failed.getRunType());
        retry.setQueueMode("collect");
        retry.setUserMessage(failed.getUserMessage());
        retry.setMergedMessageIds("[]");
        retry.setStatus(RunStatus.QUEUED.name());
        retry.setRetryCount((failed.getRetryCount() != null ? failed.getRetryCount() : 0) + 1);
        retry.setPolicyId(failed.getPolicyId());
        retry.setSourceTaskTraceId(failed.getSourceTaskTraceId());
        // snapshot.runId keeps the lineage back to the failed run.
        retry.setExecutionSnapshot(failed.getExecutionSnapshot());
        LocalDateTime now = LocalDateTime.now();
        retry.setCreatedAt(now);
        retry.setUpdatedAt(now);
        retry.setTimeoutAt(now.plusSeconds(properties.getRunTimeoutSeconds()));
        retry.setDeleted(0);
        runMapper.insert(retry);
        eventService.publish(retry.getRunId(), retry.getConversationId(), retry.getTraceId(),
                "run.queued", null, null, Map.of(
                        "retryOf", failedRunId,
                        "fromCheckpoint", StringUtils.hasText(failed.getExecutionSnapshot()),
                        "runType", retry.getRunType()));
        return retry;
    }

    /** Watchdog: PAUSING never got its snapshot — the runtime kept going. */
    public void revertPausing(String runId) {
        UpdateWrapper<AgentRun> revert = new UpdateWrapper<>();
        revert.eq("run_id", runId)
                .eq("status", RunStatus.PAUSING.name())
                .set("status", RunStatus.RUNNING.name())
                .set("updated_at", LocalDateTime.now());
        runMapper.update(null, revert);
    }

    /** Restart recovery: a PAUSING run whose snapshot landed becomes PAUSED. */
    public void settlePausedAfterRestart(String runId) {
        UpdateWrapper<AgentRun> settle = new UpdateWrapper<>();
        settle.eq("run_id", runId)
                .eq("status", RunStatus.PAUSING.name())
                .set("status", RunStatus.PAUSED.name())
                .set("updated_at", LocalDateTime.now());
        if (runMapper.update(null, settle) > 0) {
            AgentRun run = runMapper.selectById(runId);
            permitService.releaseGlobal(run.getGlobalPermitId());
            UpdateWrapper<AgentRun> clearGlobal = new UpdateWrapper<>();
            clearGlobal.eq("run_id", runId).set("global_permit_id", null);
            runMapper.update(null, clearGlobal);
            log.info("settled PAUSING run as PAUSED after restart run={}", runId);
        }
    }

    public AgentRun getRun(String runId) {
        return runMapper.selectById(runId);
    }

    public List<AgentRun> listByStatuses(List<String> statuses, int limit) {
        return runMapper.selectList(new QueryWrapper<AgentRun>()
                .in("status", statuses)
                .orderByAsc("created_at")
                .last("limit " + Math.max(1, limit)));
    }

    public boolean markStarting(String runId, String convPermitId, String globalPermitId) {
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .eq("status", RunStatus.QUEUED.name())
                .set("status", RunStatus.STARTING.name())
                .set("conv_permit_id", convPermitId)
                .set("global_permit_id", globalPermitId)
                .set("updated_at", LocalDateTime.now());
        return runMapper.update(null, update) > 0;
    }

    private String category(AgentRun run) {
        return StringUtils.hasText(run.getRunType()) ? run.getRunType() : "unknown";
    }

    private int countOccurrences(String haystack, String needle) {
        if (!StringUtils.hasText(haystack) || !StringUtils.hasText(needle)) {
            return 0;
        }
        int count = 0;
        int idx = 0;
        while ((idx = haystack.indexOf(needle, idx)) >= 0) {
            count++;
            idx += needle.length();
        }
        return count;
    }

    private JsonNode readJson(String json) {
        try {
            return json != null ? objectMapper.readTree(json) : objectMapper.createObjectNode();
        } catch (Exception e) {
            return objectMapper.createObjectNode();
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readJsonAsMap(String json) {
        try {
            return json != null ? objectMapper.readValue(json, Map.class) : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value != null ? value : Map.of());
        } catch (Exception e) {
            return "{}";
        }
    }

    private String stringOf(Object value) {
        return value != null ? String.valueOf(value) : null;
    }

    private int intOf(Object value) {
        if (value instanceof Number number) {
            return number.intValue();
        }
        try {
            return value != null ? Integer.parseInt(String.valueOf(value)) : 0;
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private Long longOf(Object value) {
        if (value instanceof Number number) {
            return number.longValue();
        }
        try {
            return value != null ? Long.parseLong(String.valueOf(value)) : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private String trim(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() > max ? text.substring(0, max) : text;
    }

    /** Duration since a timestamp, guarding nulls (watchdog helper). */
    public static long secondsSince(LocalDateTime moment) {
        return moment == null ? Long.MAX_VALUE
                : Duration.between(moment, LocalDateTime.now()).toSeconds();
    }
}
