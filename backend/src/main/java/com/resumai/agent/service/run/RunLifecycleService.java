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
    private final RunEventService eventService;
    private final RunPermitService permitService;
    private final PolicyService policyService;
    private final RewardService rewardService;
    private final AgentRuntimeClient runtimeClient;
    private final MemoryService memoryService;
    private final AgentRunProperties properties;
    private final ObjectMapper objectMapper;

    public RunLifecycleService(AgentRunMapper runMapper,
                               AgentExecutionRecordMapper executionMapper,
                               ToolCallLogMapper toolCallMapper,
                               ConversationSessionMapper sessionMapper,
                               ConversationMessageMapper messageMapper,
                               RunEventService eventService,
                               RunPermitService permitService,
                               PolicyService policyService,
                               RewardService rewardService,
                               AgentRuntimeClient runtimeClient,
                               @Lazy MemoryService memoryService,
                               AgentRunProperties properties,
                               ObjectMapper objectMapper) {
        this.runMapper = runMapper;
        this.executionMapper = executionMapper;
        this.toolCallMapper = toolCallMapper;
        this.sessionMapper = sessionMapper;
        this.messageMapper = messageMapper;
        this.eventService = eventService;
        this.permitService = permitService;
        this.policyService = policyService;
        this.rewardService = rewardService;
        this.runtimeClient = runtimeClient;
        this.memoryService = memoryService;
        this.properties = properties;
        this.objectMapper = objectMapper;
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
            PolicyService.Selection selection =
                    policyService.selectPolicy(runId, category(run), selectionContext);
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
        payload.put("resumeText", session.getResumeText());
        payload.put("jobDescription", session.getJobDescription());
        payload.put("jobCategory", session.getJobCategory());
        payload.put("conversationSummary", session.getSummary());
        payload.put("currentGoal", session.getCurrentGoal());
        payload.put("policyId", bundle.getPolicyId());
        payload.put("policyConfig", readJsonAsMap(bundle.getConfig()));
        payload.put("recentMessages", recentMessages(run.getConversationId(), 12));
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
                                String conversationSummary, String currentGoal) {
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
        RunStatus terminal = switch (result.status() != null ? result.status() : "FAILED") {
            case "SUCCEEDED", "SUCCESS", "PARTIAL_SUCCESS" -> RunStatus.SUCCEEDED;
            case "CANCELLED" -> RunStatus.CANCELLED;
            case "TIMED_OUT" -> RunStatus.TIMED_OUT;
            default -> RunStatus.FAILED;
        };
        if (RunStatus.CANCELLING.name().equals(run.getStatus()) && terminal == RunStatus.SUCCEEDED) {
            // The user asked to stop; a completion racing past the cancel does
            // not overturn it. Keep the answer for audit but finish CANCELLED.
            terminal = RunStatus.CANCELLED;
        }
        finishInternal(run, terminal, result, result.errorCode(), result.errorMessage(), null);
        return true;
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
        if (terminal == RunStatus.SUCCEEDED && result != null && StringUtils.hasText(result.answer())) {
            saveAssistantMessage(finished, result.answer());
            updateSessionAfterRun(finished, result);
        }

        // 3. emit terminal SSE event
        String eventType = switch (terminal) {
            case SUCCEEDED -> "run.completed";
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
            if (StringUtils.hasText(finished.getPolicyId())) {
                policyService.recordRunOutcome(finished.getPolicyId(), category(finished),
                        terminal == RunStatus.SUCCEEDED);
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
