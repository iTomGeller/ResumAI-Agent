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
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

@Service
public class ConversationService {

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
                               RunTypeClassifier runTypeClassifier) {
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
            sessionMapper.updateById(session);
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
        return sessionMapper.selectById(conversationId);
    }

    // ------------------------------------------------------------------
    // Turn handling
    // ------------------------------------------------------------------

    @Transactional
    public ConversationTurnResponse sendTurn(String idOrTraceId, ConversationTurnRequest request) {
        ConversationSession session = lockSession(ensureSession(idOrTraceId).getId());
        // Re-check idempotency while holding the conversation row lock. Two
        // retries with the same clientMessageId can arrive before either one
        // commits; serializing first prevents a duplicate-message exception.
        ConversationTurnResponse replay = replayIfPresent(session.getId(), request.clientMessageId());
        if (replay != null) {
            return replay;
        }
        int activeRevision = session.getActiveRevision() != null ? session.getActiveRevision() : 1;
        if (request.expectedRevision() != null && request.expectedRevision() != activeRevision) {
            throw new ApiConflictException(
                    "会话 revision 已从 v" + request.expectedRevision() + " 更新为 v" + activeRevision + "，请刷新后重试。");
        }
        ConversationIntentClassifier.Decision decision = classifier.classify(request.content());
        boolean agentRuntimeConversation = StringUtils.hasText(session.getResumeText());
        if (agentRuntimeConversation) {
            return handleRuntimeTurn(session, request, decision, activeRevision);
        }
        return handleLegacyTurn(session, request, decision, activeRevision);
    }

    /**
     * Agent-runtime conversations: every substantive message becomes (or
     * merges into) a queued run; control words map to run cancellation.
     */
    private ConversationTurnResponse handleRuntimeTurn(ConversationSession session,
                                                       ConversationTurnRequest request,
                                                       ConversationIntentClassifier.Decision decision,
                                                       int activeRevision) {
        String queueMode = request.normalizedQueueMode();
        // Explicit control: 停止/取消 cancels the active run instead of queueing.
        if ("CONTROL_COMMAND".equals(decision.intent()) && "CANCEL".equals(decision.action())) {
            saveMessage(
                    session.getId(), request.clientMessageId(), "USER", decision.intent(),
                    request.content(), activeRevision, null, queueMode,
                    Map.of("control", "CANCEL"));
            AgentRun active = runQueueService.findActiveRun(session.getId());
            String message;
            String interruptedRunId = null;
            if (active != null) {
                runLifecycleService.cancelActiveRun(active, "user_cancelled", "用户在对话中要求停止");
                interruptedRunId = active.getRunId();
                message = "已请求停止当前任务，取消完成后会通知你。";
            } else {
                AgentRun pending = runQueueService.findPendingRun(session.getId());
                if (pending != null && runQueueService.cancelQueued(pending.getRunId(), "user_cancelled")) {
                    interruptedRunId = pending.getRunId();
                    message = "已取消排队中的任务。";
                } else {
                    message = "当前没有正在运行的任务。";
                }
            }
            ConversationTurnResponse response = new ConversationTurnResponse(
                    session.getId(), request.clientMessageId(), "CONTROL_COMMAND", false, false,
                    false, "CANCEL", message, session.getActiveTraceId(), activeRevision, null,
                    List.of(), null, null, null, queueMode, interruptedRunId);
            saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                    "CONTROL_COMMAND", message, activeRevision,
                    interruptedRunId, queueMode, response);
            return response;
        }

        ConversationMessage userMessage = saveMessage(
                session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, null, queueMode,
                Map.of("affectsEvaluation", decision.affectsEvaluation()));

        int resultingRevision = activeRevision;
        if (decision.affectsEvaluation()) {
            resultingRevision = activeRevision + 1;
            session.setActiveRevision(resultingRevision);
            session.setCurrentGoal(trimTo(request.content(), 1900));
            session.setUpdateTime(LocalDateTime.now());
            sessionMapper.updateById(session);
        }

        String runType = runTypeClassifier.classify(request.content(), session.getJobCategory());
        RunQueueService.SubmitResult submit = runQueueService.submit(
                session.getId(), session.getUserId(), session.getActiveTraceId(),
                resultingRevision, runType, queueMode, request.content(), userMessage.getId(),
                request.forcedPolicyId());
        runSchedulerService.kick();

        AgentRun run = submit.run();
        int queuePosition = runQueueService.queuePosition(run);
        String receipt;
        if (submit.interruptedRun() != null) {
            receipt = "已打断当前任务并基于最新指令重新分析（Run " + shortId(run.getRunId()) + "）。";
        } else if (submit.mergedIntoExisting()) {
            receipt = "补充信息已合并进等待中的任务（Run " + shortId(run.getRunId()) + "），当前排队第 "
                    + queuePosition + " 位。";
        } else if (queuePosition > 1 || runQueueService.findActiveRun(session.getId()) != null) {
            receipt = "请求已入队（Run " + shortId(run.getRunId()) + "，第 " + queuePosition
                    + " 位），当前任务完成后自动执行。";
        } else {
            receipt = "已创建任务 Run " + shortId(run.getRunId()) + "，正在调度执行，进度会实时推送。";
        }

        ConversationTurnResponse response = new ConversationTurnResponse(
                session.getId(), request.clientMessageId(), decision.intent(),
                decision.affectsEvaluation(), false, false,
                submit.interruptedRun() != null ? "RUN_INTERRUPTED"
                        : submit.mergedIntoExisting() ? "RUN_MERGED" : "RUN_QUEUED",
                receipt, session.getActiveTraceId(), resultingRevision, null, List.of(),
                run.getRunId(), run.getStatus(), queuePosition, queueMode,
                submit.interruptedRun() != null ? submit.interruptedRun().getRunId() : null);
        saveMessage(session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT",
                decision.intent(), receipt, resultingRevision, run.getRunId(), queueMode, response);
        return response;
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
            sessionMapper.updateById(session);
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
        ConversationSession existing = sessionMapper.selectById(idOrTraceId);
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
        ConversationSession session = sessionMapper.selectById(idOrTraceId);
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
        return message;
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
