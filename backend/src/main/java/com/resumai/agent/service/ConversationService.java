package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ConversationSnapshotResponse;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.api.dto.ConversationTurnResponse;
import com.resumai.agent.api.dto.TaskControlRequest;
import com.resumai.agent.api.dto.TaskControlResponse;
import com.resumai.agent.api.ApiConflictException;
import com.resumai.agent.api.ApiNotFoundException;
import com.resumai.agent.api.dto.TaskResponse;
import com.resumai.agent.conversation.ConversationReplyService;
import com.resumai.agent.conversation.CopilotMetrics;
import com.resumai.agent.conversation.TurnDecision;
import com.resumai.agent.conversation.TurnDisposition;
import com.resumai.agent.conversation.TurnPolicyService;
import com.resumai.agent.dao.ConversationMessageMapper;
import com.resumai.agent.dao.ConversationSessionMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationMessage;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.service.run.AgentRuntimeClient;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunQueueService;
import com.resumai.agent.service.run.RunSchedulerService;
import com.resumai.agent.service.run.RunTypeClassifier;
import com.resumai.agent.util.HrContext;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import org.redisson.api.RMapCache;
import org.redisson.api.RedissonClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.util.StringUtils;

@Service
public class ConversationService {

    private static final Logger log = LoggerFactory.getLogger(ConversationService.class);

    private final ConversationSessionMapper sessionMapper;
    private final ConversationMessageMapper messageMapper;
    private final ResumeTaskMapper resumeTaskMapper;
    private final ResumeEvaluationService evaluationService;
    private final TaskControlService taskControlService;
    private final ConversationIntentClassifier classifier;
    private final AgentRuntimeClient runtimeClient;
    private final ObjectMapper objectMapper;
    private final RunQueueService runQueueService;
    private final RunSchedulerService runSchedulerService;
    private final RunLifecycleService runLifecycleService;
    private final RunTypeClassifier runTypeClassifier;
    private final TurnPolicyService turnPolicyService;
    private final ConversationReplyService conversationReplyService;
    private final ConversationTurnService conversationTurnService;
    private final RedissonClient redisson;

    @Autowired(required = false)
    private CopilotMetrics copilotMetrics;

    public ConversationService(ConversationSessionMapper sessionMapper,
                               ConversationMessageMapper messageMapper,
                               ResumeTaskMapper resumeTaskMapper,
                               ResumeEvaluationService evaluationService,
                               TaskControlService taskControlService,
                               ConversationIntentClassifier classifier,
                               AgentRuntimeClient runtimeClient,
                               ObjectMapper objectMapper,
                               RunQueueService runQueueService,
                               RunSchedulerService runSchedulerService,
                               RunLifecycleService runLifecycleService,
                               RunTypeClassifier runTypeClassifier,
                               TurnPolicyService turnPolicyService,
                               ConversationReplyService conversationReplyService,
                               ConversationTurnService conversationTurnService,
                               RedissonClient redisson) {
        this.sessionMapper = sessionMapper;
        this.messageMapper = messageMapper;
        this.resumeTaskMapper = resumeTaskMapper;
        this.evaluationService = evaluationService;
        this.taskControlService = taskControlService;
        this.classifier = classifier;
        this.runtimeClient = runtimeClient;
        this.objectMapper = objectMapper;
        this.runQueueService = runQueueService;
        this.runSchedulerService = runSchedulerService;
        this.runLifecycleService = runLifecycleService;
        this.runTypeClassifier = runTypeClassifier;
        this.turnPolicyService = turnPolicyService;
        this.conversationReplyService = conversationReplyService;
        this.conversationTurnService = conversationTurnService;
        this.redisson = redisson;
    }

    // ------------------------------------------------------------------
    // Session hot-state cache (working-memory tier): Redis holds the hot
    // conversation snapshot (goal/summary/revision) with a 7d TTL; MySQL
    // stays the source of truth — every write goes through, reads fall back.
    // ------------------------------------------------------------------

    private static final Duration SESSION_CACHE_TTL = Duration.ofDays(7);

    private RMapCache<String, String> sessionCache() {
        return redisson.getMapCache("resumai:conversation:hot");
    }

    private ConversationSession cachedSession(String conversationId) {
        try {
            String json = sessionCache().get(conversationId);
            if (json != null) {
                return objectMapper.readValue(json, ConversationSession.class);
            }
        } catch (Exception e) {
            log.debug("session cache read miss {}: {}", conversationId, e.getMessage());
        }
        ConversationSession session = sessionMapper.selectById(conversationId);
        if (session != null) {
            cacheSession(session);
        }
        return session;
    }

    private void cacheSession(ConversationSession session) {
        if (session == null || session.getId() == null) {
            return;
        }
        try {
            sessionCache().fastPut(session.getId(), objectMapper.writeValueAsString(session),
                    SESSION_CACHE_TTL.toMillis(), TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            log.debug("session cache write skipped {}: {}", session.getId(), e.getMessage());
        }
    }

    private void writeSession(ConversationSession session) {
        sessionMapper.updateById(session);
        cacheSession(session);
    }

    // ------------------------------------------------------------------
    // Conversation creation / listing (agent-runtime conversations)
    // ------------------------------------------------------------------

    public record CreateConversationRequest(String title, String resumeText, String jobDescription,
                                            String jobCategory, String fromTraceId) {
    }

    @Transactional
    public ConversationSession createConversation(CreateConversationRequest request) {
        String resumeText = request.resumeText();
        String jobDescription = request.jobDescription();
        String jobCategory = request.jobCategory();
        String traceId = "ct-" + UUID.randomUUID();
        if (StringUtils.hasText(request.fromTraceId())) {
            ResumeTask task = resumeTaskMapper.selectOne(new QueryWrapper<ResumeTask>()
                    .eq("trace_id", request.fromTraceId()).last("limit 1"));
            if (task == null) {
                throw new ApiNotFoundException("找不到评估任务：" + request.fromTraceId());
            }
            if (!StringUtils.hasText(resumeText)) {
                resumeText = task.getResumeText();
            }
            if (!StringUtils.hasText(jobDescription)) {
                jobDescription = task.getJobDescription();
            }
            if (!StringUtils.hasText(jobCategory)) {
                jobCategory = task.getJobCategory();
            }
            traceId = task.getTraceId();
        }
        if (!StringUtils.hasText(resumeText)) {
            throw new IllegalArgumentException("resumeText 不能为空（可上传简历或引用已有任务）");
        }
        LocalDateTime now = LocalDateTime.now();
        ConversationSession session = new ConversationSession();
        session.setId("conv-" + UUID.randomUUID());
        session.setUserId(HrContext.getHrId());
        session.setTitle(StringUtils.hasText(request.title()) ? request.title()
                : "简历会话 " + now.toLocalDate());
        session.setResumeText(resumeText);
        session.setJobDescription(jobDescription);
        session.setJobCategory(jobCategory);
        session.setSummaryVersion(0);
        session.setActiveTraceId(traceId);
        session.setActiveRevision(1);
        session.setTenantId("default");
        session.setCreatedBy(HrContext.getHrId());
        session.setCreateTime(now);
        session.setUpdateTime(now);
        session.setDeleted(0);
        sessionMapper.insert(session);
        return session;
    }

    public List<ConversationSession> listConversations(String userId, int limit) {
        QueryWrapper<ConversationSession> query = new QueryWrapper<>();
        if (StringUtils.hasText(userId)) {
            query.eq("user_id", userId);
        }
        query.orderByDesc("update_time").last("limit " + Math.min(Math.max(limit, 1), 100));
        return sessionMapper.selectList(query);
    }

    @Transactional
    public void attachContext(String conversationId, String resumeText, String jobDescription,
                              String jobCategory) {
        ConversationSession session = lockSession(ensureSession(conversationId).getId());
        boolean dirty = false;
        if (StringUtils.hasText(resumeText)) {
            session.setResumeText(resumeText);
            dirty = true;
        }
        if (StringUtils.hasText(jobDescription)) {
            session.setJobDescription(jobDescription);
            dirty = true;
        }
        if (StringUtils.hasText(jobCategory)) {
            session.setJobCategory(jobCategory);
            dirty = true;
        }
        if (dirty) {
            session.setUpdateTime(LocalDateTime.now());
            writeSession(session);
        }
    }

    // ------------------------------------------------------------------
    // Snapshot
    // ------------------------------------------------------------------

    @Transactional
    public ConversationSnapshotResponse getSnapshot(String idOrTraceId) {
        ConversationSession session = ensureSession(idOrTraceId);
        List<ConversationMessage> messages = messageMapper.selectList(
                new QueryWrapper<ConversationMessage>()
                        .eq("conversation_id", session.getId())
                        .orderByAsc("id"));
        List<ResumeTask> revisions = resumeTaskMapper.selectList(
                new QueryWrapper<ResumeTask>()
                        .and(w -> w.eq("conversation_id", session.getId())
                                .or().eq("trace_id", session.getActiveTraceId()))
                        .orderByAsc("revision_no", "create_time"));
        return new ConversationSnapshotResponse(
                session.getId(),
                session.getActiveTraceId(),
                session.getActiveRevision(),
                messages.stream().map(this::toMessageView).toList(),
                revisions.stream().map(this::toRevisionView).toList());
    }

    public ConversationSession getSession(String conversationId) {
        return cachedSession(conversationId);
    }

    // ------------------------------------------------------------------
    // Turn handling
    // ------------------------------------------------------------------

    @Transactional
    public ConversationTurnResponse sendTurn(String idOrTraceId, ConversationTurnRequest request) {
        return sendTurnInternal(idOrTraceId, request, null, System.nanoTime());
    }

    @Transactional
    public ConversationTurnResponse sendTurn(String idOrTraceId,
                                             ConversationTurnRequest request,
                                             Consumer<String> onDelta) {
        return sendTurnInternal(idOrTraceId, request, onDelta, System.nanoTime());
    }

    @Transactional
    public ConversationTurnResponse sendTurn(String idOrTraceId,
                                             ConversationTurnRequest request,
                                             Consumer<String> onDelta,
                                             long requestAcceptedNanos) {
        return sendTurnInternal(
                idOrTraceId, request, onDelta, requestAcceptedNanos);
    }

    private ConversationTurnResponse sendTurnInternal(
            String idOrTraceId, ConversationTurnRequest request,
            Consumer<String> onDelta, long requestAcceptedNanos) {
        recordPipelineStage("requestToTransactionalEntry", requestAcceptedNanos);
        long stageStarted = System.nanoTime();
        ConversationSession ensured = ensureSession(idOrTraceId);
        recordPipelineStage("sessionResolve", stageStarted);
        stageStarted = System.nanoTime();
        ConversationSession session = lockSession(ensured.getId());
        recordPipelineStage("sessionRowLock", stageStarted);
        // Re-check idempotency while holding the conversation row lock. Two
        // retries with the same clientMessageId can arrive before either one
        // commits; serializing first prevents a duplicate-message exception.
        stageStarted = System.nanoTime();
        ConversationTurnResponse replay = replayIfPresent(
                session.getId(), request.clientMessageId());
        recordPipelineStage("idempotencyLookup", stageStarted);
        if (replay != null) {
            return replay;
        }
        int activeRevision = session.getActiveRevision() != null ? session.getActiveRevision() : 1;
        if (request.expectedRevision() != null && request.expectedRevision() != activeRevision) {
            throw new ApiConflictException(
                    "会话 revision 已从 v" + request.expectedRevision() + " 更新为 v" + activeRevision + "，请刷新后重试。");
        }
        stageStarted = System.nanoTime();
        ConversationIntentClassifier.Decision decision = classifier.classify(request.content());
        recordPipelineStage("ruleClassification", stageStarted);
        boolean agentRuntimeConversation = StringUtils.hasText(session.getResumeText());
        if (agentRuntimeConversation) {
            return handleRuntimeTurn(
                    session, request, activeRevision, onDelta,
                    requestAcceptedNanos);
        }
        return handleLegacyTurn(session, request, decision, activeRevision);
    }

    /**
     * Agent-runtime conversations: TurnPolicyService owns disposition.
     * Ordinary chat → DIRECT_REPLY (CopilotAnswer), never an evaluation AgentRun.
     */
    private ConversationTurnResponse handleRuntimeTurn(ConversationSession session,
                                                       ConversationTurnRequest request,
                                                       int activeRevision,
                                                       Consumer<String> onDelta,
                                                       long requestAcceptedNanos) {
        long stageStarted = System.nanoTime();
        RunQueueService.ConversationRunState runState =
                runQueueService.findConversationRunState(session.getId());
        AgentRun active = runState.active();
        if (active == null) {
            // A paused run is still the active immutable revision. Evaluation-
            // changing input must supersede it instead of leaving a stale
            // checkpoint that can later overwrite the new intent.
            active = runState.paused();
        }
        AgentRun pending = runState.pending();
        recordPipelineStage("runStateLookup", stageStarted);
        stageStarted = System.nanoTime();
        TurnDecision decision = turnPolicyService.decide(session, active, pending, request.content());
        recordPipelineStage("turnPolicy", stageStarted);

        return switch (decision.disposition()) {
            case CONTROL -> handleControlDisposition(session, request, decision, activeRevision);
            case DIRECT_REPLY -> handleDirectReply(
                    session, request, decision, activeRevision, false, onDelta,
                    requestAcceptedNanos);
            case BACKGROUND_QUERY -> handleDirectReply(
                    session, request, decision, activeRevision, true, onDelta,
                    requestAcceptedNanos);
            case MERGE_CONTEXT -> handleMergeContext(session, request, decision, activeRevision, pending);
            case CREATE_REVISION -> handleEvaluationRevision(session, request, decision,
                    activeRevision, false);
            case SUPERSEDE_RUN -> handleEvaluationRevision(session, request, decision,
                    activeRevision, true);
        };
    }

    private ConversationTurnResponse handleControlDisposition(ConversationSession session,
                                                              ConversationTurnRequest request,
                                                              TurnDecision decision,
                                                              int activeRevision) {
        String action = StringUtils.hasText(decision.controlAction())
                ? decision.controlAction() : "CANCEL";
        saveMessage(session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, null, null,
                Map.of("disposition", decision.disposition().name(), "control", action));
        AgentRun active = runQueueService.findActiveRun(session.getId());
        AgentRun paused = runQueueService.findPausedRun(session.getId());
        String message;
        String interruptedRunId = null;
        if ("CANCEL".equals(action)) {
            AgentRun target = active != null ? active : paused;
            if (target != null) {
                runLifecycleService.cancelActiveRun(
                        target, "user_cancelled", "用户在对话中要求停止");
                interruptedRunId = target.getRunId();
                message = active != null
                        ? "已请求停止当前任务，取消完成后会通知你。"
                        : "已立即取消暂停中的任务。";
            } else {
                AgentRun pending = runQueueService.findPendingRun(session.getId());
                if (pending != null && runQueueService.cancelQueued(pending.getRunId(), "user_cancelled")) {
                    interruptedRunId = pending.getRunId();
                    message = "已取消排队中的任务。";
                } else {
                    message = "当前没有正在运行的任务。";
                }
            }
        } else if ("PAUSE".equals(action) && active != null) {
            try {
                AgentRun pausing = runLifecycleService.pauseActiveRun(
                        active, "用户在对话中要求暂停");
                interruptedRunId = pausing.getRunId();
                message = "已请求暂停；当前节点结束后会在安全边界写入 checkpoint。";
            } catch (Exception e) {
                log.warn("pause via conversation failed run={}: {}", active.getRunId(), e.getMessage());
                message = "暂停请求失败：" + e.getMessage();
            }
        } else if ("RESUME".equals(action) && paused != null) {
            try {
                AgentRun resumed = runLifecycleService.resumePausedRun(paused);
                message = "已从 checkpoint 继续当前任务。";
                interruptedRunId = resumed.getRunId();
            } catch (Exception e) {
                log.warn("resume via conversation failed run={}: {}", paused.getRunId(), e.getMessage());
                message = "继续任务失败：" + e.getMessage();
            }
        } else {
            message = "当前没有可执行该控制指令的任务。";
        }
        ConversationTurnResponse response = new ConversationTurnResponse(
                session.getId(), request.clientMessageId(), decision.intent(), false, false,
                false, action, message, session.getActiveTraceId(), activeRevision, null,
                List.of(), null, null, null, null, interruptedRunId,
                decision.disposition().name(), decision.reason(), null,
                List.of(), List.of(), List.of());
        saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                "CONTROL_COMMAND", message, activeRevision, interruptedRunId, null, response);
        return response;
    }

    private ConversationTurnResponse handleDirectReply(ConversationSession session,
                                                       ConversationTurnRequest request,
                                                       TurnDecision decision,
                                                       int activeRevision,
                                                       boolean allowTools,
                                                       Consumer<String> onDelta,
                                                       long requestAcceptedNanos) {
        // DIRECT_REPLY / BACKGROUND_QUERY always persist conversation_turn and
        // never create agent_run (runId stays null).
        long stageStarted = System.nanoTime();
        var persistedTurn = conversationTurnService.start(
                session.getId(),
                request.clientMessageId(),
                decision.disposition().name(),
                decision.intent(),
                request.content());
        recordPipelineStage("conversationTurnStart", stageStarted);

        if (decision.needsConfirmation()) {
            saveMessage(session.getId(), request.clientMessageId(), "USER", decision.intent(),
                    request.content(), activeRevision, null, null,
                    Map.of("needsConfirmation", true, "disposition", decision.disposition().name()));
            String clarify = "你是想把当前主评估切换到这个方向，还是只想顺便比较一下？当前评估会继续运行。";
            conversationTurnService.complete(persistedTurn.getTurnId(), clarify, List.of(), List.of());
            ConversationTurnResponse response = new ConversationTurnResponse(
                    session.getId(), request.clientMessageId(), decision.intent(),
                    false, true, true, "ASK_CONFIRMATION", clarify,
                    session.getActiveTraceId(), activeRevision, null, List.of(),
                    null, null, null, null, null,
                    decision.disposition().name(), decision.reason(), persistedTurn.getTurnId(),
                    List.of(), List.of(), List.of());
            saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                    decision.intent(), clarify, activeRevision, null, null, response);
            return response;
        }

        stageStarted = System.nanoTime();
        saveMessage(session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, null, null,
                Map.of("disposition", decision.disposition().name(),
                        "affectsEvaluation", false));
        recordPipelineStage("userMessageInsert", stageStarted);
        if (copilotMetrics != null) {
            copilotMetrics.recordPipelineStage(
                    "requestToReplyService",
                    Math.max(0, TimeUnit.NANOSECONDS.toMillis(
                            System.nanoTime() - requestAcceptedNanos)));
        }

        ConversationReplyService.CopilotReply reply;
        try {
            reply = conversationReplyService.reply(
                    session, request, decision, allowTools,
                    persistedTurn.getTurnId(), onDelta);
            conversationTurnService.complete(
                    persistedTurn.getTurnId(), reply.answer(), reply.citations(), reply.actions());
        } catch (RuntimeException ex) {
            conversationTurnService.fail(persistedTurn.getTurnId(), ex.getMessage());
            throw ex;
        }

        ConversationTurnResponse response = new ConversationTurnResponse(
                session.getId(), request.clientMessageId(), decision.intent(),
                false, true, false,
                allowTools ? "BACKGROUND_QUERY" : "DIRECT_REPLY",
                reply.answer(), session.getActiveTraceId(), activeRevision, null, List.of(),
                null, null, null, null, null,
                decision.disposition().name(), decision.reason(), persistedTurn.getTurnId(),
                reply.citations(), reply.actions(), reply.suggestions());
        saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                decision.intent(), reply.answer(), activeRevision, null, null, response);
        return response;
    }

    private void recordPipelineStage(String stage, long startedNanos) {
        if (copilotMetrics == null) {
            return;
        }
        copilotMetrics.recordPipelineStage(
                stage,
                Math.max(0, TimeUnit.NANOSECONDS.toMillis(
                        System.nanoTime() - startedNanos)));
    }

    private ConversationTurnResponse handleMergeContext(ConversationSession session,
                                                        ConversationTurnRequest request,
                                                        TurnDecision decision,
                                                        int activeRevision,
                                                        AgentRun pending) {
        ConversationMessage userMessage = saveMessage(
                session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, null, null,
                Map.of("disposition", decision.disposition().name(), "merged", true));

        RunQueueService.SubmitResult submit = runQueueService.submitEvaluationRun(
                session.getId(), session.getUserId(), session.getActiveTraceId(),
                activeRevision,
                evaluationRunType(request.content(), session.getJobCategory()),
                false, request.content(), userMessage.getId());
        runSchedulerService.kick();

        AgentRun run = submit.run();
        int queuePosition = runQueueService.queuePosition(run);
        String receipt = submit.mergedIntoExisting()
                ? "补充信息已自动合并进等待中的任务（Run " + shortId(run.getRunId()) + "）。"
                : "补充信息已入队合并执行（Run " + shortId(run.getRunId()) + "）。";

        ConversationTurnResponse response = new ConversationTurnResponse(
                session.getId(), request.clientMessageId(), decision.intent(),
                true, false, false, "RUN_MERGED", receipt,
                session.getActiveTraceId(), activeRevision, null, List.of(),
                run.getRunId(), run.getStatus(), queuePosition, null, null,
                decision.disposition().name(), decision.reason(), null,
                List.of(), List.of(Map.of("type", "UNDO_MERGE", "label", "撤销合并")),
                List.of());
        saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                decision.intent(), receipt, activeRevision, run.getRunId(), null, response);
        return response;
    }

    private ConversationTurnResponse handleEvaluationRevision(ConversationSession session,
                                                              ConversationTurnRequest request,
                                                              TurnDecision decision,
                                                              int activeRevision,
                                                              boolean supersede) {
        ConversationMessage userMessage = saveMessage(
                session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, null, null,
                Map.of("disposition", decision.disposition().name(),
                        "affectsEvaluation", true, "supersede", supersede));

        int resultingRevision = activeRevision + 1;
        session.setActiveRevision(resultingRevision);
        session.setCurrentGoal(trimTo(request.content(), 1900));
        String requestedCategory = inferRequestedCategory(request.content());
        if (StringUtils.hasText(requestedCategory)) {
            session.setJobCategory(requestedCategory);
        }
        String requestedJd = extractExplicitJd(request.content());
        if (StringUtils.hasText(requestedJd)) {
            session.setJobDescription(requestedJd);
        }
        session.setUpdateTime(LocalDateTime.now());
        writeSession(session);

        String runType = evaluationRunType(request.content(), session.getJobCategory());
        AgentRun activeInterrupted = supersede
                ? runQueueService.findActiveRun(session.getId()) : null;
        if (activeInterrupted != null) {
            // Propagate cancellation to the Python worker immediately. Merely
            // flipping MySQL to CANCELLING would let the stale revision keep
            // spending tokens until the watchdog grace period elapsed.
            runLifecycleService.cancelActiveRun(
                    activeInterrupted,
                    "superseded_by_new_revision",
                    "运行中的旧 revision 已被用户新意图替代");
        }
        AgentRun pausedInterrupted = runQueueService.findPausedRun(session.getId());
        if (pausedInterrupted != null) {
            runLifecycleService.cancelActiveRun(
                    pausedInterrupted,
                    "superseded_by_new_revision",
                    "暂停中的旧 revision 已被用户新意图替代");
        }
        RunQueueService.SubmitResult submit = runQueueService.submitEvaluationRun(
                session.getId(), session.getUserId(), session.getActiveTraceId(),
                resultingRevision, runType, supersede, request.content(), userMessage.getId());
        runSchedulerService.kick();

        AgentRun run = submit.run();
        AgentRun interruptedRun = submit.interruptedRun() != null
                ? submit.interruptedRun()
                : activeInterrupted != null ? activeInterrupted : pausedInterrupted;
        int queuePosition = runQueueService.queuePosition(run);
        String receipt;
        if (interruptedRun != null) {
            receipt = "已自动替换当前评估并基于最新指令重新分析（Run " + shortId(run.getRunId()) + "）。";
        } else if (queuePosition > 1 || runQueueService.findActiveRun(session.getId()) != null) {
            receipt = "已创建评估 revision v" + resultingRevision + "（Run "
                    + shortId(run.getRunId()) + "，队列第 " + queuePosition + " 位）。";
        } else {
            receipt = "已创建评估 revision v" + resultingRevision + "（Run "
                    + shortId(run.getRunId()) + "），正在调度执行。";
        }

        ConversationTurnResponse response = new ConversationTurnResponse(
                session.getId(), request.clientMessageId(), decision.intent(),
                true, false, false,
                supersede ? "RUN_SUPERSEDED" : "REVISION_CREATED",
                receipt, session.getActiveTraceId(), resultingRevision, null,
                decision.invalidatedArtifacts(),
                run.getRunId(), run.getStatus(), queuePosition, null,
                interruptedRun != null ? interruptedRun.getRunId() : null,
                decision.disposition().name(), decision.reason(), null,
                List.of(),
                List.of(Map.of("type", "OPEN_REPORT", "label", "打开决策报告")),
                List.of());
        saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                decision.intent(), receipt, resultingRevision, run.getRunId(), null, response);
        return response;
    }

    /** Only evaluation dispositions may classify a multi-agent run type. */
    private String evaluationRunType(String content, String jobCategory) {
        String runType = runTypeClassifier.classify(content, jobCategory);
        if ("quick_answer".equals(runType)) {
            return "full_evaluation";
        }
        return runType;
    }

    /** Legacy trace-driven conversations keep the original side-quest/revision flow. */
    private ConversationTurnResponse handleLegacyTurn(ConversationSession session,
                                                      ConversationTurnRequest request,
                                                      ConversationIntentClassifier.Decision localDecision,
                                                      int activeRevision) {
        RuntimeTurn resolved = resolveRuntimeTurn(session, request.content(), localDecision);
        ConversationIntentClassifier.Decision decision = resolved.decision();
        saveMessage(
                session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, null, null,
                Map.of("affectsEvaluation", decision.affectsEvaluation()));

        String oldTraceId = session.getActiveTraceId();
        String activeTraceId = oldTraceId;
        int resultingRevision = activeRevision;
        String action = decision.action();
        String assistantMessage = StringUtils.hasText(resolved.assistantMessage())
                ? resolved.assistantMessage()
                : fallbackAssistantMessage(session, request.content(), decision);

        if ("CONTROL_COMMAND".equals(decision.intent())) {
            TaskControlRequest.Action controlAction = TaskControlRequest.Action.valueOf(decision.action());
            TaskControlResponse controlled = taskControlService.control(oldTraceId, controlAction);
            action = controlled.action();
            assistantMessage = controlled.message();
        } else if (decision.affectsEvaluation() && !decision.needsConfirmation()) {
            resultingRevision = activeRevision + 1;
            String requestedCategory = inferRequestedCategory(request.content());
            String requestedJd = extractExplicitJd(request.content());
            TaskResponse created = evaluationService.createRevision(
                    oldTraceId,
                    session.getId(),
                    resultingRevision,
                    requestedCategory,
                    requestedJd,
                    request.content(),
                    decision.affectedNodes(),
                    "GOAL_CHANGE".equals(decision.intent()));
            activeTraceId = created.traceId();
            session.setActiveTraceId(activeTraceId);
            session.setActiveRevision(resultingRevision);
            session.setUpdateTime(LocalDateTime.now());
            writeSession(session);
            action = "REVISION_CREATED";
            assistantMessage += " 当前有效版本为 v" + resultingRevision + "。";
        }

        ConversationTurnResponse response = ConversationTurnResponse.legacy(
                session.getId(), request.clientMessageId(), decision.intent(),
                decision.affectsEvaluation(), decision.answerThenResume(), decision.needsConfirmation(),
                action, assistantMessage, activeTraceId, resultingRevision,
                !oldTraceId.equals(activeTraceId) ? oldTraceId : null,
                decision.affectedNodes());
        saveMessage(
                session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT", decision.intent(),
                assistantMessage, resultingRevision, null, null, response);
        return response;
    }

    private ConversationSession ensureSession(String idOrTraceId) {
        ConversationSession existing = cachedSession(idOrTraceId);
        if (existing != null) {
            return existing;
        }
        ResumeTask task = resumeTaskMapper.selectOne(
                new QueryWrapper<ResumeTask>()
                        .and(w -> w.eq("trace_id", idOrTraceId).or().eq("conversation_id", idOrTraceId))
                        .orderByDesc("revision_no", "create_time")
                        .last("limit 1"));
        if (task == null) {
            throw new ApiNotFoundException("找不到会话或评估任务：" + idOrTraceId);
        }
        String conversationId = StringUtils.hasText(task.getConversationId())
                ? task.getConversationId() : task.getTraceId();
        existing = sessionMapper.selectById(conversationId);
        if (existing != null) {
            return existing;
        }
        LocalDateTime now = LocalDateTime.now();
        ConversationSession session = new ConversationSession();
        session.setId(conversationId);
        session.setUserId(StringUtils.hasText(task.getUploadedBy()) ? task.getUploadedBy() : HrContext.getHrId());
        session.setActiveTraceId(task.getTraceId());
        session.setActiveRevision(task.getRevisionNo() != null ? task.getRevisionNo() : 1);
        session.setResumeText(task.getResumeText());
        session.setJobDescription(task.getJobDescription());
        session.setJobCategory(task.getJobCategory());
        session.setSummaryVersion(0);
        session.setTenantId(StringUtils.hasText(task.getTenantId()) ? task.getTenantId() : "default");
        session.setCreatedBy(StringUtils.hasText(task.getUploadedBy()) ? task.getUploadedBy() : HrContext.getHrId());
        session.setCreateTime(now);
        session.setUpdateTime(now);
        session.setDeleted(0);
        try {
            sessionMapper.insert(session);
        } catch (DuplicateKeyException concurrentBootstrap) {
            ConversationSession winner = sessionMapper.selectById(conversationId);
            if (winner != null) {
                return winner;
            }
            throw concurrentBootstrap;
        }

        if (!StringUtils.hasText(task.getConversationId())) {
            task.setConversationId(conversationId);
            task.setRevisionNo(1);
            if (!StringUtils.hasText(task.getWorkflowRunId())) {
                task.setWorkflowRunId(task.getTraceId());
            }
            resumeTaskMapper.updateById(task);
        }
        return session;
    }

    private ConversationSession lockSession(String conversationId) {
        ConversationSession session = sessionMapper.selectOne(
                new QueryWrapper<ConversationSession>()
                        .eq("id", conversationId)
                        .last("for update"));
        if (session == null) {
            throw new ApiNotFoundException("会话不存在：" + conversationId);
        }
        return session;
    }

    private ConversationTurnResponse replayIfPresent(String idOrTraceId, String clientMessageId) {
        ConversationSession session = cachedSession(idOrTraceId);
        if (session == null) {
            return null;
        }
        ConversationMessage assistant = messageMapper.selectOne(
                new QueryWrapper<ConversationMessage>()
                        .eq("conversation_id", session.getId())
                        .eq("client_message_id", clientMessageId + ":assistant")
                        .last("limit 1"));
        if (assistant == null || !StringUtils.hasText(assistant.getMetadataJson())) {
            return null;
        }
        try {
            return objectMapper.readValue(assistant.getMetadataJson(), ConversationTurnResponse.class);
        } catch (Exception ignored) {
            return null;
        }
    }

    // EXP-6: data-driven floor, see harness/run_intent_eval.py
    @org.springframework.beans.factory.annotation.Value("${resumai.intent.confidence-floor:0.7}")
    private double intentConfidenceFloor;

    /** EXP-6 evaluation hook — rule layer only, no side effects. */
    public ConversationIntentClassifier.Decision classifyRuleOnly(String content) {
        return classifier.classify(content);
    }

    /**
     * LLM second pass for messages the rule layer could not classify.
     * Timeout/unavailability degrades back to the rule default; a low LLM
     * confidence turns into a clarification instead of a guess.
     */
    private ConversationIntentClassifier.Decision llmIntentSecondPass(
            ConversationSession session, String content,
            ConversationIntentClassifier.Decision ruleDecision) {
        try {
            Map<String, Object> request = new LinkedHashMap<>();
            request.put("conversationId", session.getId());
            request.put("traceId", session.getActiveTraceId());
            request.put("revision", session.getActiveRevision());
            request.put("content", content);
            request.put("runStatus", "RUNTIME");
            Map<String, Object> context = new LinkedHashMap<>();
            context.put("activeGoal", session.getCurrentGoal());
            context.put("summary", session.getSummary());
            context.put("runStatus", "RUNTIME");
            request.put("context", context);
            Optional<Map<String, Object>> runtime = runtimeClient.resolveConversationTurn(request);
            if (runtime.isEmpty()) {
                return ruleDecision;
            }
            Map<String, Object> payload = runtime.get();
            double confidence = payload.get("confidence") instanceof Number n
                    ? n.doubleValue() : 1.0;
            String intent = String.valueOf(payload.getOrDefault("intent", ruleDecision.intent()));
            boolean affects = booleanValue(payload.get("affectsEvaluation"),
                    ruleDecision.affectsEvaluation());
            if (confidence < intentConfidenceFloor) {
                return new ConversationIntentClassifier.Decision(
                        intent, false, false, true, "ASK_CONFIRMATION", List.of(),
                        "我不完全确定你的意图：是想补充当前评估的信息，还是提出一个新的目标？"
                                + "如果要改评估方向，请明确说“改为……重新评估”。");
            }
            boolean confirmation = booleanValue(payload.get("requiresConfirmation"), false);
            return new ConversationIntentClassifier.Decision(
                    intent, affects, !affects, confirmation,
                    affects ? "CREATE_REVISION" : "ANSWER_AND_CONTINUE",
                    stringList(payload.get("affectedNodes")),
                    ruleDecision.defaultMessage());
        } catch (Exception e) {
            log.debug("llm intent second pass degraded to rules: {}", e.getMessage());
            return ruleDecision;
        }
    }

    private RuntimeTurn resolveRuntimeTurn(ConversationSession session,
                                           String content,
                                           ConversationIntentClassifier.Decision localDecision) {
        if ("CONTROL_COMMAND".equals(localDecision.intent())) {
            return new RuntimeTurn(localDecision, localDecision.defaultMessage());
        }
        ResumeTask active = evaluationService.loadResumeTaskRow(session.getActiveTraceId()).orElse(null);
        TaskResponse activeView;
        try {
            activeView = evaluationService.getTask(session.getActiveTraceId());
        } catch (Exception missing) {
            return new RuntimeTurn(localDecision, null);
        }
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("conversationId", session.getId());
        request.put("traceId", session.getActiveTraceId());
        request.put("revision", session.getActiveRevision());
        request.put("content", content);
        request.put("workflowRunId", active != null ? active.getWorkflowRunId() : null);
        request.put("runStatus", active != null ? active.getStatus() : "UNKNOWN");
        Map<String, Object> context = new LinkedHashMap<>();
        context.put("activeGoal", active != null ? active.getEvaluationBrief() : "");
        context.put("currentSummary", active != null ? active.getSummary() : "");
        context.put("summary", active != null ? active.getSummary() : "");
        context.put("runStatus", active != null ? active.getStatus() : "UNKNOWN");
        context.put("topJdMatches", activeView.topJdMatches());
        context.put("interviewQuestions", activeView.interviewQuestions());
        context.put("risks", activeView.risks());
        request.put("context", context);
        Optional<Map<String, Object>> runtime = runtimeClient.resolveConversationTurn(request);
        if (runtime.isEmpty()) {
            return new RuntimeTurn(localDecision, null);
        }
        Map<String, Object> payload = runtime.get();
        String intent = String.valueOf(payload.getOrDefault("intent", localDecision.intent()));
        String controlAction = payload.get("controlAction") != null
                ? String.valueOf(payload.get("controlAction")).trim().toUpperCase(Locale.ROOT) : "";
        if (Set.of("PAUSE", "RESUME", "CANCEL").contains(controlAction)) {
            ConversationIntentClassifier.Decision control = new ConversationIntentClassifier.Decision(
                    "CONTROL_COMMAND", false, "RESUME".equals(controlAction), false,
                    controlAction, List.of(),
                    String.valueOf(payload.getOrDefault("assistantMessage", localDecision.defaultMessage())));
            return new RuntimeTurn(control, control.defaultMessage());
        }
        boolean affects = booleanValue(payload.get("affectsEvaluation"), localDecision.affectsEvaluation());
        boolean confirmation = booleanValue(payload.get("requiresConfirmation"), localDecision.needsConfirmation());
        boolean answerThenResume = booleanValue(payload.get("answerThenResume"), !affects);
        List<String> affectedNodes = stringList(payload.get("affectedNodes"));
        if (affects && affectedNodes.isEmpty()) {
            affectedNodes = localDecision.affectedNodes();
        }
        String action = affects
                ? "CREATE_REVISION"
                : confirmation ? "ASK_CONFIRMATION" : "ANSWER_AND_CONTINUE";
        Object answerValue = payload.get("assistantMessage");
        String runtimeAnswer = answerValue != null ? String.valueOf(answerValue).trim() : null;
        ConversationIntentClassifier.Decision decision = new ConversationIntentClassifier.Decision(
                intent, affects, answerThenResume, confirmation, action,
                affectedNodes, StringUtils.hasText(runtimeAnswer) && !"null".equals(runtimeAnswer)
                        ? runtimeAnswer : localDecision.defaultMessage());
        return new RuntimeTurn(decision, runtimeAnswer);
    }

    private String fallbackAssistantMessage(ConversationSession session,
                                            String content,
                                            ConversationIntentClassifier.Decision decision) {
        if (!decision.answerThenResume()) {
            return decision.defaultMessage();
        }
        ResumeTask active = evaluationService.loadResumeTaskRow(session.getActiveTraceId()).orElse(null);
        if (content.contains("进度") || content.contains("到哪")) {
            return active == null
                    ? "当前评估状态暂不可用；主任务没有被中断。"
                    : "当前状态：" + active.getStatus() + "。" + Optional.ofNullable(active.getSummary()).orElse("");
        }
        return decision.defaultMessage();
    }

    private boolean booleanValue(Object value, boolean fallback) {
        return value instanceof Boolean bool ? bool : fallback;
    }

    private List<String> stringList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        return list.stream().map(String::valueOf).filter(StringUtils::hasText).toList();
    }

    private String inferRequestedCategory(String content) {
        String lower = content.toLowerCase(Locale.ROOT);
        if (lower.contains("前端")) return "FRONTEND";
        if (lower.contains("后端")) return "BACKEND";
        if (lower.contains("算法") || lower.contains("ai") || lower.contains("机器学习")) return "AI_ML";
        if (lower.contains("数据") || lower.contains("数仓")) return "DATA";
        if (lower.contains("测试") || lower.contains("qa")) return "QA";
        if (lower.contains("运维") || lower.contains("devops") || lower.contains("sre")) return "DEVOPS";
        if (lower.contains("全栈")) return "FULLSTACK";
        if (lower.contains("安全")) return "SECURITY";
        if (lower.contains("嵌入式") || lower.contains("客户端")) return "CLIENT";
        if (lower.contains("产品")) return "PRODUCT";
        if (lower.contains("设计")) return "DESIGN";
        if (lower.contains("运营")) return "OPERATION";
        return null;
    }

    private String extractExplicitJd(String content) {
        String lower = content.toLowerCase(Locale.ROOT);
        int index = lower.indexOf("jd:");
        if (index < 0) index = lower.indexOf("jd：");
        if (index < 0) return null;
        String jd = content.substring(index + 3).trim();
        return StringUtils.hasText(jd) ? jd : null;
    }

    private ConversationMessage saveMessage(String conversationId, String clientMessageId, String role,
                                            String intent, String content, int revision,
                                            String runId, String queueMode, Object metadata) {
        ConversationMessage message = new ConversationMessage();
        message.setConversationId(conversationId);
        message.setClientMessageId(clientMessageId);
        message.setRole(role);
        message.setIntentType(intent);
        message.setContent(content);
        message.setRevisionNo(revision);
        message.setRunId(runId);
        message.setQueueMode(queueMode);
        message.setMetadataJson(writeJson(metadata));
        message.setCreateTime(LocalDateTime.now());
        message.setDeleted(0);
        messageMapper.insert(message);
        if ("ASSISTANT".equalsIgnoreCase(role)) {
            refreshCopilotCacheAfterCommit(conversationId);
        }
        return message;
    }

    private void refreshCopilotCacheAfterCommit(String conversationId) {
        Runnable refresh = () -> conversationReplyService.refreshHistoryCache(
                cachedSession(conversationId));
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(
                    new TransactionSynchronization() {
                        @Override
                        public void afterCommit() {
                            refresh.run();
                        }
                    });
        } else {
            refresh.run();
        }
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value != null ? value : Map.of());
        } catch (Exception e) {
            return "{}";
        }
    }

    private String shortId(String runId) {
        if (runId == null) {
            return "";
        }
        return runId.length() > 12 ? runId.substring(runId.length() - 8) : runId;
    }

    private String trimTo(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() > max ? text.substring(0, max) : text;
    }

    private ConversationSnapshotResponse.Message toMessageView(ConversationMessage message) {
        return new ConversationSnapshotResponse.Message(
                message.getId(), message.getClientMessageId(), message.getRole(), message.getIntentType(),
                message.getContent(), message.getRevisionNo(), message.getCreateTime());
    }

    private ConversationSnapshotResponse.Revision toRevisionView(ResumeTask task) {
        return new ConversationSnapshotResponse.Revision(
                task.getTraceId(), task.getRevisionNo() != null ? task.getRevisionNo() : 1,
                task.getStatus(), task.getWorkflowRunId(), task.getSupersedesTraceId(),
                task.getSupersededByTraceId(), task.getEvaluationBrief(), task.getCreateTime());
    }

    private record RuntimeTurn(
            ConversationIntentClassifier.Decision decision,
            String assistantMessage
    ) {
    }
}
