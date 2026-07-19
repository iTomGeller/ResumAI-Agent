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
import com.resumai.agent.domain.entity.ConversationMessage;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.util.HrContext;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
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
    private final WorkflowClient workflowClient;
    private final ObjectMapper objectMapper;

    public ConversationService(ConversationSessionMapper sessionMapper,
                               ConversationMessageMapper messageMapper,
                               ResumeTaskMapper resumeTaskMapper,
                               ResumeEvaluationService evaluationService,
                               TaskControlService taskControlService,
                               ConversationIntentClassifier classifier,
                               WorkflowClient workflowClient,
                               ObjectMapper objectMapper) {
        this.sessionMapper = sessionMapper;
        this.messageMapper = messageMapper;
        this.resumeTaskMapper = resumeTaskMapper;
        this.evaluationService = evaluationService;
        this.taskControlService = taskControlService;
        this.classifier = classifier;
        this.workflowClient = workflowClient;
        this.objectMapper = objectMapper;
    }

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

        ConversationIntentClassifier.Decision localDecision = classifier.classify(request.content());
        RuntimeTurn resolved = resolveRuntimeTurn(session, request.content(), localDecision);
        ConversationIntentClassifier.Decision decision = resolved.decision();
        saveMessage(
                session.getId(), request.clientMessageId(), "USER", decision.intent(),
                request.content(), activeRevision, Map.of("affectsEvaluation", decision.affectsEvaluation()));

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

        ConversationTurnResponse response = new ConversationTurnResponse(
                session.getId(), request.clientMessageId(), decision.intent(),
                decision.affectsEvaluation(), decision.answerThenResume(), decision.needsConfirmation(),
                action, assistantMessage, activeTraceId, resultingRevision,
                !oldTraceId.equals(activeTraceId) ? oldTraceId : null,
                decision.affectedNodes());
        saveMessage(
                session.getId(), request.clientMessageId() + ":assistant", "ASSISTANT", decision.intent(),
                assistantMessage, resultingRevision, response);
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
        session.setActiveTraceId(task.getTraceId());
        session.setActiveRevision(task.getRevisionNo() != null ? task.getRevisionNo() : 1);
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
        TaskResponse activeView = evaluationService.getTask(session.getActiveTraceId());
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
        Optional<Map<String, Object>> runtime = workflowClient.resolveConversationTurn(request);
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

    private void saveMessage(String conversationId, String clientMessageId, String role,
                             String intent, String content, int revision, Object metadata) {
        ConversationMessage message = new ConversationMessage();
        message.setConversationId(conversationId);
        message.setClientMessageId(clientMessageId);
        message.setRole(role);
        message.setIntentType(intent);
        message.setContent(content);
        message.setRevisionNo(revision);
        message.setMetadataJson(writeJson(metadata));
        message.setCreateTime(LocalDateTime.now());
        message.setDeleted(0);
        messageMapper.insert(message);
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "{}";
        }
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
