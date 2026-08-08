package com.resumai.agent.service;

import com.resumai.agent.api.dto.TaskResponse;
import com.resumai.agent.api.ApiConflictException;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.baomidou.mybatisplus.core.toolkit.IdWorker;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.resumai.agent.api.dto.CreateTaskRequest;
import com.resumai.agent.util.HrContext;
import com.resumai.agent.util.MarkdownTextUtil;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import com.resumai.agent.api.dto.DashboardMetricsResponse;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.TaskListItemResponse;
import com.resumai.agent.api.dto.TaskQueueFields;
import com.resumai.agent.api.dto.TraceEventResponse;
import com.resumai.agent.dao.AgentExecutionTraceMapper;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.ConversationSessionMapper;
import com.resumai.agent.dao.DynamicSkillPromptMapper;
import com.resumai.agent.dao.MetaEvolutionHistoryMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.dao.SystemOrchestrationRuleMapper;
import com.resumai.agent.api.dto.TaskResponse.TaskSystemError;
import com.resumai.agent.domain.entity.AgentExecutionTrace;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.entity.DynamicSkillPrompt;
import com.resumai.agent.domain.entity.MetaEvolutionHistory;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.EvolutionType;
import com.resumai.agent.domain.enums.QueueStatus;
import com.resumai.agent.domain.enums.RunStatus;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunQueueService;
import com.resumai.agent.service.run.RunSchedulerService;
import com.resumai.agent.domain.dag.DagStepRegistry;
import com.resumai.agent.domain.dag.DagStepRegistry.StepDefinition;
import com.resumai.agent.domain.entity.SystemOrchestrationRule;
import com.resumai.agent.rag.RagOptions;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Lazy;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;
import org.springframework.web.multipart.MultipartFile;

/**
 * 简历评估核心服务 — 管理评估任务生命周期、Agent执行追踪与持久化。
 *
 * <p>MySQL 承担任务/JD/Trace 的事实查询，内存与 Redis 仅承接 RUNNING 运行态缓存。</p>
 */
@Service
public class ResumeEvaluationService {

    private static final Logger log = LoggerFactory.getLogger(ResumeEvaluationService.class);
    private static final long MAX_UPLOAD_BYTES = 20L * 1024L * 1024L;
    private static final int MAX_RESUME_TEXT_LENGTH = 20000;
    private static final Set<String> TERMINAL_TASK_STATUSES = Set.of(
            "SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "SUPERSEDED");
    private static final Set<String> REVISIONABLE_TASK_STATUSES = Set.of(
            "QUEUED", "RUNNING", "PAUSING", "PAUSED", "RESUMING");
    private static final Set<String> CALLBACK_ACTIVE_STATUSES = Set.of(
            "RUNNING", "PAUSING", "RESUMING");

    private final AtomicLong taskId = new AtomicLong(1000);
    private final Map<String, MutableTask> tasks = new ConcurrentHashMap<>();
    private final Map<String, List<TraceEventResponse>> traces = new ConcurrentHashMap<>();
    private final Map<String, AtomicInteger> traceSequences = new ConcurrentHashMap<>();
    private final Map<String, Map<String, AtomicInteger>> traceRoundCounters = new ConcurrentHashMap<>();
    private final TaskQueueService taskQueueService;
    private final SseTraceHub sseTraceHub;
    private final ResumeRagService resumeRagService;
    private final ResumeTaskMapper resumeTaskMapper;
    private final AgentRunMapper agentRunMapper;
    private final AgentExecutionTraceMapper agentExecutionTraceMapper;
    private final MetaEvolutionHistoryMapper metaEvolutionHistoryMapper;
    private final SystemOrchestrationRuleMapper ruleMapper;
    private final DynamicSkillPromptMapper skillPromptMapper;
    private final ExternalProfileService externalProfileService;
    private final JdRagService jdRagService;
    private final HybridRagService hybridRagService;
    private final RagConfigService ragConfigService;
    private final ResumeFileService resumeFileService;
    private final TaskQueryService taskQueryService;
    private final RuntimeStateService runtimeStateService;
    private final ObjectMapper objectMapper;
    private final RunQueueService runQueueService;
    private final RunSchedulerService runSchedulerService;
    private final RunLifecycleService runLifecycleService;
    private final ConversationSessionMapper conversationSessionMapper;
    private final CandidateService candidateService;

    public ResumeEvaluationService(SseTraceHub sseTraceHub,
                                ResumeRagService resumeRagService,
                                ResumeTaskMapper resumeTaskMapper,
                                AgentRunMapper agentRunMapper,
                                AgentExecutionTraceMapper agentExecutionTraceMapper,
                                MetaEvolutionHistoryMapper metaEvolutionHistoryMapper,
                                SystemOrchestrationRuleMapper ruleMapper,
                                DynamicSkillPromptMapper skillPromptMapper,
                                ExternalProfileService externalProfileService,
                                JdRagService jdRagService,
                                HybridRagService hybridRagService,
                                RagConfigService ragConfigService,
                                ResumeFileService resumeFileService,
                                TaskQueryService taskQueryService,
                                RuntimeStateService runtimeStateService,
                                TaskQueueService taskQueueService,
                                ObjectMapper objectMapper,
                                RunQueueService runQueueService,
                                @Lazy RunSchedulerService runSchedulerService,
                                @Lazy RunLifecycleService runLifecycleService,
                                ConversationSessionMapper conversationSessionMapper,
                                CandidateService candidateService) {
        this.sseTraceHub = sseTraceHub;
        this.resumeRagService = resumeRagService;
        this.resumeTaskMapper = resumeTaskMapper;
        this.agentRunMapper = agentRunMapper;
        this.agentExecutionTraceMapper = agentExecutionTraceMapper;
        this.metaEvolutionHistoryMapper = metaEvolutionHistoryMapper;
        this.ruleMapper = ruleMapper;
        this.skillPromptMapper = skillPromptMapper;
        this.externalProfileService = externalProfileService;
        this.jdRagService = jdRagService;
        this.hybridRagService = hybridRagService;
        this.ragConfigService = ragConfigService;
        this.resumeFileService = resumeFileService;
        this.taskQueryService = taskQueryService;
        this.runtimeStateService = runtimeStateService;
        this.taskQueueService = taskQueueService;
        this.objectMapper = objectMapper;
        this.runQueueService = runQueueService;
        this.runSchedulerService = runSchedulerService;
        this.runLifecycleService = runLifecycleService;
        this.conversationSessionMapper = conversationSessionMapper;
        this.candidateService = candidateService;
    }

    @PostConstruct
    void restorePersistedState() {
        initTaskIdFromDb();
        restoreTasksFromDb();
    }

    /**
     * A linked run finished and mirrored its outcome into resume_task (DB).
     * Refresh the in-memory task cache from the authoritative row so the task
     * list, dashboard counters and detail view flip without a restart.
     */
    @org.springframework.context.event.EventListener
    public void onTaskRunSynced(RunLifecycleService.TaskRunSyncedEvent event) {
        try {
            ResumeTask row = loadResumeTaskRow(event.traceId()).orElse(null);
            if (row == null) {
                return;
            }
            // Full rehydrate from the authoritative row: score, recommendation,
            // structured report and lists all flow into the cache in one shot.
            if (StringUtils.hasText(row.getResultPayload())) {
                try {
                    Map<String, Object> payload = objectMapper.readValue(
                            row.getResultPayload(), new TypeReference<>() {});
                    MutableTask hydrated = hydrateTaskFromPayload(row, payload);
                    tasks.put(event.traceId(), hydrated);
                } catch (Exception e) {
                    log.debug("rehydrate failed trace={}: {}", event.traceId(), e.getMessage());
                }
            }
            MutableTask cached = tasks.get(event.traceId());
            LocalDateTime now = LocalDateTime.now();
            if (cached != null) {
                synchronized (cached) {
                    cached.status = row.getStatus();
                    cached.queueStatus = row.getQueueStatus();
                    cached.summary = row.getSummary();
                    cached.overallScore = row.getOverallScore();
                    cached.recommendation = StringUtils.hasText(row.getRecommendation())
                            ? row.getRecommendation() : null;
                    cached.durationMs = row.getDurationMs() != null ? row.getDurationMs() : cached.durationMs;
                    cached.tokenCost = row.getTokenCost() != null ? row.getTokenCost() : cached.tokenCost;
                    cached.finishedAt = row.getFinishedAt() != null ? row.getFinishedAt() : now;
                    cached.updateTime = now;
                }
            }
            runtimeStateService.evictRunningTask(event.traceId());
        } catch (Exception e) {
            log.debug("task cache refresh failed trace={}: {}", event.traceId(), e.getMessage());
        }
    }

    private void initTaskIdFromDb() {
        try {
            ResumeTask maxRow = resumeTaskMapper.selectOne(
                    new QueryWrapper<ResumeTask>().orderByDesc("id").last("limit 1"));
            if (maxRow != null && maxRow.getId() != null && maxRow.getId() > taskId.get()) {
                taskId.set(maxRow.getId());
            }
        } catch (Exception e) {
            log.warn("[eval] init task id from db failed: {}", e.getMessage());
        }
    }

    private void restoreTasksFromDb() {
        try {
            QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
            wrapper.orderByDesc("create_time").last("limit 200");
            List<ResumeTask> rows = resumeTaskMapper.selectList(wrapper);
            for (ResumeTask row : rows) {
                if (!StringUtils.hasText(row.getTraceId()) || tasks.containsKey(row.getTraceId())) {
                    continue;
                }
                if (!StringUtils.hasText(row.getResultPayload())) {
                    continue;
                }
                Map<String, Object> payload = objectMapper.readValue(row.getResultPayload(), new TypeReference<>() {});
                MutableTask task = hydrateTaskFromPayload(row, payload);
                tasks.put(task.traceId, task);
                traces.putIfAbsent(task.traceId, new ArrayList<>());
                if (row.getId() != null && row.getId() > taskId.get()) {
                    taskId.set(row.getId());
                }
            }
        } catch (Exception e) {
            log.warn("[eval] restore tasks from db failed: {}", e.getMessage());
        }
    }

    @SuppressWarnings("unchecked")
    private MutableTask hydrateTaskFromPayload(ResumeTask row, Map<String, Object> payload) {
        LocalDateTime createTime = row.getCreateTime() != null ? row.getCreateTime() : LocalDateTime.now();
        LocalDateTime updateTime = row.getUpdateTime() != null ? row.getUpdateTime() : createTime;
        MutableTask task = new MutableTask(
                row.getId(),
                row.getTraceId(),
                stringValue(payload.get("fileName"), row.getCandidateName()),
                stringValue(payload.get("jobCategory"), row.getJobCategory()),
                stringValue(payload.get("executionMode"), row.getExecutionMode()),
                stringValue(payload.get("status"), row.getStatus()),
                nullableInteger(payload.get("overallScore"), row.getOverallScore()),
                nullableString(payload.get("recommendation"), row.getRecommendation()),
                stringValue(payload.get("summary"), ""),
                longValue(payload.get("durationMs"), 0L),
                intValue(payload.get("tokenCost"), 0),
                createTime,
                updateTime,
                stringList(payload.get("strengths")),
                stringList(payload.get("risks")),
                stringList(payload.get("interviewQuestions")),
                stringValue(payload.get("jobDescription"), row.getJobDescription()),
                stringValue(payload.get("resumeText"), row.getResumeText()),
                stringValue(payload.get("resumeFilePath"), row.getFileUrl()),
                stringValue(payload.get("resumeFileType"), null),
                stringValue(payload.get("matchedJdTitle"), null),
                doubleValue(payload.get("jdMatchScore")),
                null,
                stringValue(payload.get("aiRecommendation"), null),
                stringValue(payload.get("decisionRationale"), null),
                stringValue(payload.get("riskSummary"), null)
        );
        task.finalReport = stringValue(payload.get("fullReport"), stringValue(payload.get("summary"), ""));
        Object structured = payload.get("structuredReport");
        if (structured instanceof Map<?, ?>) {
            task.structuredReport = objectMapper.convertValue(structured, new TypeReference<>() {});
        }
        Object topMatches = payload.get("topJdMatches");
        if (topMatches instanceof List<?>) {
            task.topJdMatches = objectMapper.convertValue(topMatches, new TypeReference<>() {});
        }
        applyQueueFieldsFromRow(task, row);
        applyRevisionFieldsFromRow(task, row);
        applyCandidateFieldsFromRow(task, row);
        return task;
    }

    private void applyCandidateFieldsFromRow(MutableTask task, ResumeTask row) {
        if (row == null) {
            return;
        }
        task.candidateId = row.getCandidateId();
        task.applicationId = row.getApplicationId();
    }

    private void applyQueueFieldsFromRow(MutableTask task, ResumeTask row) {
        if (row == null) {
            return;
        }
        task.uploadedBy = row.getUploadedBy();
        task.tenantId = row.getTenantId();
        task.priority = row.getPriority() != null ? row.getPriority() : 0;
        task.queueStatus = row.getQueueStatus();
        task.queuedAt = row.getQueuedAt();
        task.startedAt = row.getStartedAt();
        task.finishedAt = row.getFinishedAt();
        task.attemptCount = row.getAttemptCount() != null ? row.getAttemptCount() : 0;
        task.nextRetryAt = row.getNextRetryAt();
        task.workerId = row.getWorkerId();
    }

    private void applyRevisionFieldsFromRow(MutableTask task, ResumeTask row) {
        if (row == null) {
            return;
        }
        task.conversationId = StringUtils.hasText(row.getConversationId())
                ? row.getConversationId() : row.getTraceId();
        task.revisionNo = row.getRevisionNo() != null ? row.getRevisionNo() : 1;
        task.workflowRunId = StringUtils.hasText(row.getWorkflowRunId())
                ? row.getWorkflowRunId() : row.getTraceId();
        task.baseWorkflowRunId = row.getBaseWorkflowRunId();
        task.supersedesTraceId = row.getSupersedesTraceId();
        task.supersededByTraceId = row.getSupersededByTraceId();
        task.evaluationBrief = row.getEvaluationBrief();
        task.invalidatedNodes = parseJsonStringList(row.getInvalidatedNodes());
        if (StringUtils.hasText(row.getRagOptions())) {
            try {
                task.ragOptions = objectMapper.readValue(row.getRagOptions(), RagOptions.class);
            } catch (Exception e) {
                log.debug("[eval] ignored invalid persisted rag options trace={}", row.getTraceId());
            }
        }
    }

    /**
     * 创建评估任务并异步执行 Agent 流程。
     *
     * @param request 创建任务请求
     * @return 创建后的任务响应
     */
    public TaskResponse createTask(CreateTaskRequest request) {
        return createTaskInternal(request, "trace-" + UUID.randomUUID(), null, null, null, null);
    }

    private TaskResponse createTaskInternal(CreateTaskRequest request,
                                            String traceId,
                                            String resumeFilePath,
                                            String resumeObjectKey,
                                            String resumeFileType,
                                            List<JdMatchResult> precomputedJdMatches) {
        return createTaskInternal(request, traceId, resumeFilePath, resumeObjectKey,
                resumeFileType, precomputedJdMatches, null);
    }

    private TaskResponse createTaskInternal(CreateTaskRequest request,
                                            String traceId,
                                            String resumeFilePath,
                                            String resumeObjectKey,
                                            String resumeFileType,
                                            List<JdMatchResult> precomputedJdMatches,
                                            RevisionContext revisionContext) {
        LocalDateTime now = LocalDateTime.now();
        String jobDescription = request.jobDescription();
        String jobCategory = normalizeJobCategory(request.jobCategory());
        String matchedJdTitle = null;
        Double jdMatchScore = null;
        List<JdMatchResult> topJdMatches = precomputedJdMatches;
        if (precomputedJdMatches != null && !precomputedJdMatches.isEmpty()) {
            JdMatchResult best = precomputedJdMatches.get(0);
            matchedJdTitle = best.title();
            jdMatchScore = best.matchScore();
            jobCategory = StringUtils.hasText(best.category()) ? best.category() : jobCategory;
            String jdDesc = jdRagService.getJdDescription(best.jdId());
            if (StringUtils.hasText(jdDesc)) {
                jobDescription = jdDesc;
            } else if (StringUtils.hasText(best.title())) {
                jobDescription = best.title();
            }
        }
        if (StringUtils.hasText(resumeFilePath) && !StringUtils.hasText(resumeFileType)) {
            resumeFileType = detectFileType(request.fileName());
        }
        MutableTask task = new MutableTask(
                taskId.incrementAndGet(),
                traceId,
                request.fileName(),
                jobCategory,
                normalizeExecutionMode(request.executionMode()),
                QueueStatus.QUEUED.name(),
                null,
                null,
                "任务已进入队列，等待后台评估。",
                0L,
                0,
                now,
                now,
                new ArrayList<>(),
                new ArrayList<>(),
                new ArrayList<>(),
                jobDescription,
                request.resumeText(),
                resumeFilePath,
                resumeFileType,
                matchedJdTitle,
                jdMatchScore,
                topJdMatches,
                null,
                null,
                null
        );
        task.uploadedBy = HrContext.getHrId();
        task.tenantId = "default";
        task.priority = 0;
        task.queueStatus = QueueStatus.QUEUED.name();
        task.queuedAt = now;
        task.attemptCount = 0;
        RagOptions ragOptions = request.ragOptions() != null ? request.ragOptions() : ragConfigService.getDefaultOptions();
        task.ragOptions = ragOptions;
        task.conversationId = revisionContext != null ? revisionContext.conversationId() : traceId;
        task.revisionNo = revisionContext != null ? revisionContext.revisionNo() : 1;
        task.workflowRunId = "run-" + UUID.randomUUID();
        task.baseWorkflowRunId = revisionContext != null ? revisionContext.baseWorkflowRunId() : null;
        task.supersedesTraceId = revisionContext != null ? revisionContext.supersedesTraceId() : null;
        task.evaluationBrief = revisionContext != null ? revisionContext.evaluationBrief() : "";
        task.invalidatedNodes = revisionContext != null && revisionContext.invalidatedNodes() != null
                ? List.copyOf(revisionContext.invalidatedNodes()) : List.of();
        persistResumeTask(task, resumeObjectKey);
        tasks.put(traceId, task);
        traces.put(traceId, new ArrayList<>());
        appendDagTrace(task.traceId, null, "CoordinatorAgent", "TASK_CREATED",
                "任务创建", "TraceId 已生成，任务已进入 Redis Stream 队列。", "SUCCESS", 18L, 0,
                null, null, "task_create", "BOTH",
                "创建评估任务", "系统已接收简历并加入评估队列", null,
                "CoordinatorAgent / TaskBootstrap", null, null, null, null, null, null);
        if (StringUtils.hasText(resumeFilePath)) {
            int textLen = request.resumeText() != null ? request.resumeText().length() : 0;
            appendDagTrace(task.traceId, null, "CoordinatorAgent", "UPLOAD_PARSE",
                    "文件解析", "简历文件已接收并完成文本抽取", "SUCCESS", 0L, 0,
                    null, null, "upload_parse", "BOTH",
                    "上传解析", "文件已保存，正文已抽取，等待后台评估", null,
                    "CoordinatorAgent / UploadHandler", null, null,
                    trim(request.fileName(), 80),
                    "文本长度: " + textLen,
                    null, null, null);
        }
        enqueueAfterCommit(task);
        return toResponse(task);
    }

    private void enqueueAfterCommit(MutableTask task) {
        Runnable enqueue = () -> taskQueueService.enqueue(
                task.traceId, task.id, task.tenantId, task.uploadedBy, task.priority);
        runAfterCommit(enqueue);
    }

    private void runAfterCommit(Runnable action) {
        if (TransactionSynchronizationManager.isActualTransactionActive()
                && TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    action.run();
                }
            });
            return;
        }
        action.run();
    }

    /**
     * Create an immutable evaluation revision while retaining the original task
     * as audit history. Only the affected graph nodes are invalidated.
     */
    @Transactional
    public TaskResponse createRevision(String sourceTraceId,
                                       String conversationId,
                                       int revisionNo,
                                       String requestedJobCategory,
                                       String requestedJobDescription,
                                       String evaluationBrief,
                                       List<String> invalidatedNodes,
                                       boolean goalChanged) {
        MutableTask source = ensureMutableTask(sourceTraceId);
        synchronized (source) {
            ResumeTask sourceRow = loadResumeTaskRow(sourceTraceId)
                    .orElseThrow(() -> new ApiConflictException("源评估任务不存在：" + sourceTraceId));
            String nextTraceId = "trace-" + UUID.randomUUID();
            LocalDateTime now = LocalDateTime.now();
            String supersededSummary = "已被会话中的 revision v" + revisionNo + " 替代。";

            UpdateWrapper<ResumeTask> supersede = new UpdateWrapper<>();
            supersede.eq("trace_id", sourceTraceId)
                    .in("status", REVISIONABLE_TASK_STATUSES)
                    .set("status", "SUPERSEDED")
                    .set("queue_status", QueueStatus.SUPERSEDED.name())
                    .set("superseded_by_trace_id", nextTraceId)
                    .set("summary", supersededSummary)
                    .set("finished_at", now)
                    .set("update_time", now);
            if (resumeTaskMapper.update(null, supersede) == 0) {
                String current = loadResumeTaskRow(sourceTraceId)
                        .map(ResumeTask::getStatus).orElse("MISSING");
                throw new ApiConflictException(
                        "源评估任务状态已变化，无法创建 revision：" + current);
            }

            String previousStatus = source.status;
            String previousQueueStatus = source.queueStatus;
            String previousSupersededBy = source.supersededByTraceId;
            String previousSummary = source.summary;
            LocalDateTime previousFinishedAt = source.finishedAt;
            LocalDateTime previousUpdateTime = source.updateTime;
            source.status = "SUPERSEDED";
            source.queueStatus = QueueStatus.SUPERSEDED.name();
            source.supersededByTraceId = nextTraceId;
            source.summary = supersededSummary;
            source.finishedAt = now;
            source.updateTime = now;
            updateResumeTask(source);

            try {
                CreateTaskRequest request = new CreateTaskRequest(
                        source.fileName,
                        StringUtils.hasText(requestedJobCategory) ? requestedJobCategory : source.jobCategory,
                        source.executionMode,
                        StringUtils.hasText(requestedJobDescription)
                                ? requestedJobDescription
                                : goalChanged ? null : source.jobDescription,
                        source.resumeText,
                        source.ragOptions
                );
                RevisionContext context = new RevisionContext(
                        StringUtils.hasText(conversationId) ? conversationId : source.conversationId,
                        revisionNo,
                        sourceTraceId,
                        source.workflowRunId,
                        evaluationBrief,
                        invalidatedNodes
                );
                TaskResponse created = createTaskInternal(
                        request,
                        nextTraceId,
                        source.resumeFilePath,
                        sourceRow.getResumeObjectKey(),
                        source.resumeFileType,
                        source.topJdMatches,
                        context
                );
                if (TransactionSynchronizationManager.isActualTransactionActive()
                        && TransactionSynchronizationManager.isSynchronizationActive()) {
                    TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                        @Override
                        public void afterCompletion(int status) {
                            if (status == TransactionSynchronization.STATUS_ROLLED_BACK) {
                                restoreRevisionMirror(
                                        source, nextTraceId, previousStatus, previousQueueStatus,
                                        previousSupersededBy, previousSummary, previousFinishedAt,
                                        previousUpdateTime);
                            }
                        }
                    });
                }
                runAfterCommit(() -> cancelLinkedRun(
                        source.workflowRunId, "superseded_by_new_revision",
                        "被新 revision 取代，已取消"));
                return created;
            } catch (RuntimeException e) {
                // The surrounding transaction rolls the database CAS back. Keep
                // the process-local mirror aligned as well.
                restoreRevisionMirror(
                        source, nextTraceId, previousStatus, previousQueueStatus,
                        previousSupersededBy, previousSummary, previousFinishedAt,
                        previousUpdateTime);
                throw e;
            }
        }
    }

    private void restoreRevisionMirror(MutableTask source,
                                       String nextTraceId,
                                       String previousStatus,
                                       String previousQueueStatus,
                                       String previousSupersededBy,
                                       String previousSummary,
                                       LocalDateTime previousFinishedAt,
                                       LocalDateTime previousUpdateTime) {
        synchronized (source) {
            source.status = previousStatus;
            source.queueStatus = previousQueueStatus;
            source.supersededByTraceId = previousSupersededBy;
            source.summary = previousSummary;
            source.finishedAt = previousFinishedAt;
            source.updateTime = previousUpdateTime;
            tasks.remove(nextTraceId);
            traces.remove(nextTraceId);
            traceSequences.remove(nextTraceId);
            traceRoundCounters.remove(nextTraceId);
            if (Set.of("RUNNING", "PAUSING", "RESUMING").contains(previousStatus)) {
                runtimeStateService.cacheRunningTask(toResponse(source));
            } else {
                runtimeStateService.evictRunningTask(source.traceId);
            }
        }
    }

    /**
     * Worker 消费入口：确保任务在内存中并开始评估。
     */
    public void runQueuedEvaluation(String traceId) {
        MutableTask task = ensureMutableTask(traceId);
        synchronized (task) {
            ResumeTask persisted = loadResumeTaskRow(traceId).orElse(null);
            if (persisted == null
                    || !"RUNNING".equals(persisted.getStatus())
                    || !QueueStatus.RUNNING.name().equals(persisted.getQueueStatus())) {
                log.info("[eval] skip stale queue claim trace={} status={} queueStatus={}", traceId,
                        persisted != null ? persisted.getStatus() : "MISSING",
                        persisted != null ? persisted.getQueueStatus() : "MISSING");
                return;
            }
            task.status = "RUNNING";
            task.queueStatus = QueueStatus.RUNNING.name();
            task.startedAt = persisted.getStartedAt() != null ? persisted.getStartedAt() : LocalDateTime.now();
            task.summary = "Agent 正在启动评估流程。";
            task.updateTime = LocalDateTime.now();
            runtimeStateService.cacheRunningTask(toResponse(task));
            // Holding the per-task monitor closes the local cancel/start gap:
            // cancellation either wins before this point or waits until the
            // workflow has been registered and can then cancel the real run.
            executeTask(task);
        }
    }

    public TaskResponse updateControlState(String traceId, String status, String queueStatus, String summary) {
        MutableTask task = ensureMutableTask(traceId);
        synchronized (task) {
        String previousStatus = task.status;
        task.status = status;
        task.queueStatus = queueStatus;
        task.summary = summary;
        task.updateTime = LocalDateTime.now();
        if (Set.of("CANCELLED", "SUPERSEDED").contains(status)) {
            task.finishedAt = task.updateTime;
        }
        if ("RUNNING".equals(status) && task.startedAt == null) {
            task.startedAt = task.updateTime;
        }
        if ("RUNNING".equals(status)) {
            task.finishedAt = null;
            runtimeStateService.cacheRunningTask(toResponse(task));
            if ("PAUSED".equals(previousStatus)) {
            }
        }
        if ("RESUMING".equals(status)) {
            task.finishedAt = null;
            runtimeStateService.cacheRunningTask(toResponse(task));
            if ("PAUSED".equals(previousStatus)) {
            }
        }
        if ("PAUSED".equals(status) && "RESUMING".equals(previousStatus)) {
            runtimeStateService.evictRunningTask(task.traceId);
        }
        if (Set.of("CANCELLED", "SUPERSEDED").contains(status)
                && Set.of("RUNNING", "PAUSING").contains(previousStatus)) {
            runtimeStateService.evictRunningTask(task.traceId);
        }
        updateResumeTask(task);
        return toResponse(task);
        }
    }

    public boolean compareAndSetControlState(String traceId,
                                             Set<String> expectedStatuses,
                                             String status,
                                             String queueStatus,
                                             String summary) {
        if (expectedStatuses == null || expectedStatuses.isEmpty()) {
            return false;
        }
        LocalDateTime now = LocalDateTime.now();
        MutableTask task = ensureMutableTask(traceId);
        synchronized (task) {
            String previousStatus = task.status;
            boolean terminal = TERMINAL_TASK_STATUSES.contains(status);
            UpdateWrapper<ResumeTask> update = new UpdateWrapper<>();
            update.eq("trace_id", traceId)
                    .in("status", expectedStatuses)
                    .set("status", status)
                    .set("queue_status", queueStatus)
                    .set("summary", summary)
                    .set("finished_at", terminal ? now : null)
                    .set("update_time", now);
            if (resumeTaskMapper.update(null, update) == 0) {
                return false;
            }
            task.status = status;
            task.queueStatus = queueStatus;
            task.summary = summary;
            task.finishedAt = terminal ? now : null;
            task.updateTime = now;
            if (Set.of("RUNNING", "RESUMING", "PAUSING").contains(status)) {
                if (task.startedAt == null) {
                    task.startedAt = now;
                }
                runtimeStateService.cacheRunningTask(toResponse(task));
            } else {
                runtimeStateService.evictRunningTask(task.traceId);
            }
            if (("PAUSED".equals(status) || terminal)
                    && Set.of("RUNNING", "PAUSING", "RESUMING").contains(previousStatus)) {
            }
        }
        return true;
    }

    public Optional<ResumeTask> loadResumeTaskRow(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return Optional.empty();
        }
        ResumeTask row = resumeTaskMapper.selectOne(new QueryWrapper<ResumeTask>().eq("trace_id", traceId).last("limit 1"));
        return Optional.ofNullable(row);
    }

    private MutableTask ensureMutableTask(String traceId) {
        MutableTask cached = tasks.get(traceId);
        if (cached != null) {
            return cached;
        }
        ResumeTask row = loadResumeTaskRow(traceId)
                .orElseThrow(() -> new IllegalArgumentException("任务不存在：" + traceId));
        if (StringUtils.hasText(row.getResultPayload())) {
            try {
                Map<String, Object> payload = objectMapper.readValue(row.getResultPayload(), new TypeReference<>() {});
                MutableTask hydrated = hydrateTaskFromPayload(row, payload);
                tasks.put(traceId, hydrated);
                traces.putIfAbsent(traceId, new ArrayList<>());
                return hydrated;
            } catch (Exception e) {
                log.warn("[eval] hydrate queued task failed (trace={}): {}", traceId, e.getMessage());
            }
        }
        LocalDateTime now = LocalDateTime.now();
        MutableTask task = new MutableTask(
                row.getId(), row.getTraceId(),
                StringUtils.hasText(row.getFileName()) ? row.getFileName() : row.getCandidateName(),
                row.getJobCategory(), row.getExecutionMode(), row.getStatus(),
                row.getOverallScore(),
                row.getRecommendation(), row.getSummary(),
                row.getDurationMs() != null ? row.getDurationMs() : 0L,
                row.getTokenCost() != null ? row.getTokenCost() : 0,
                row.getCreateTime() != null ? row.getCreateTime() : now,
                row.getUpdateTime() != null ? row.getUpdateTime() : now,
                new ArrayList<>(), new ArrayList<>(), new ArrayList<>(),
                row.getJobDescription(), row.getResumeText(), row.getFileUrl(), null,
                row.getMatchedJdTitle(), row.getJdMatchScore(), null,
                null, null, null
        );
        applyQueueFieldsFromRow(task, row);
        applyRevisionFieldsFromRow(task, row);
        applyCandidateFieldsFromRow(task, row);
        tasks.put(traceId, task);
        traces.putIfAbsent(traceId, new ArrayList<>());
        return task;
    }

    public TaskListItemResponse toListItemFromEntity(ResumeTask row) {
        return taskQueryService.toListItem(row);
    }

    /**
     * 从上传文件中抽取简历正文并创建评估任务。
     *
     * <p>真实 HR 使用场景通常从 PDF 简历开始，不能要求用户先手工复制文本。
     * 当前支持 PDF、TXT、Markdown 和 CSV；Word 简历会给出明确错误，
     * 避免把不可解析内容静默送入 Agent 导致假评估。</p>
     *
     * @param file 简历文件
     * @param jobCategory 岗位类别
     * @param executionMode 执行模式
     * @param jobDescription 岗位描述
     * @return 创建后的任务响应
     */
    public TaskResponse createTaskFromUpload(MultipartFile file,
                                             String jobCategory,
                                             String executionMode,
                                             String jobDescription) {
        return createTaskFromUpload(file, jobCategory, executionMode, jobDescription, false);
    }

    public TaskResponse createTaskFromUpload(MultipartFile file,
                                             String jobCategory,
                                             String executionMode,
                                             String jobDescription,
                                             boolean planMode) {
        String fileName = file == null ? "" : file.getOriginalFilename();
        String normalizedCategory = normalizeJobCategory(jobCategory);
        String fileType = detectFileType(fileName);
        String resumeText = extractResumeText(file, fileType, normalizedCategory);
        String traceId = "trace-" + UUID.randomUUID();
        ResumeFileService.SavedResumeFile saved = resumeFileService.save(traceId, file, fileType);
        CreateTaskRequest request = new CreateTaskRequest(
                StringUtils.hasText(fileName) ? fileName : "uploaded-resume",
                jobCategory,
                executionMode,
                jobDescription,
                resumeText
        );
        TaskResponse created = createTaskInternal(request, traceId, saved.localPath(), null, fileType, null);
        markPlanMode(traceId, planMode);
        return created;
    }

    /**
     * Upload resume with automatic JD matching deferred to async DAG execution.
     * Returns immediately after file save and text extraction; JD matching runs in background.
     */
    public TaskResponse createTaskFromUploadAutoMatch(MultipartFile file, String executionMode) {
        return createTaskFromUploadAutoMatch(file, executionMode, false);
    }

    public TaskResponse createTaskFromUploadAutoMatch(MultipartFile file, String executionMode,
                                                      boolean planMode) {
        String fileName = file == null ? "" : file.getOriginalFilename();
        String fileType = detectFileType(fileName);
        String resumeText = extractResumeText(file, fileType, "AUTO");
        String traceId = "trace-" + UUID.randomUUID();
        ResumeFileService.SavedResumeFile saved = resumeFileService.save(traceId, file, fileType);
        CreateTaskRequest request = new CreateTaskRequest(
                StringUtils.hasText(fileName) ? fileName : "uploaded-resume",
                "AUTO",
                executionMode,
                "",
                resumeText
        );
        // Hybrid-RRF 预匹配：创建时写入 matched_jd / topJdMatches，与 runtime jd-search 同源。
        List<JdMatchResult> matches = List.of();
        try {
            matches = hybridRagService.retrieve(resumeText, ragConfigService.getDefaultOptions());
        } catch (Exception e) {
            log.warn("[eval] hybrid JD pre-match skipped: {}", e.getMessage());
        }
        TaskResponse created = createTaskInternal(request, traceId, saved.localPath(), null,
                fileType, matches.isEmpty() ? null : matches);
        markPlanMode(traceId, planMode);
        return created;
    }

    /** Plan-approval flag lives on the in-memory task until the run enqueues. */
    private void markPlanMode(String traceId, boolean planMode) {
        if (!planMode) {
            return;
        }
        MutableTask task = tasks.get(traceId);
        if (task != null) {
            task.planMode = true;
        }
    }

    public Path getResumeFile(String traceId) {
        MutableTask task = tasks.get(traceId);
        if (task != null && StringUtils.hasText(task.resumeFilePath)) {
            Path path = Path.of(task.resumeFilePath).normalize();
            if (java.nio.file.Files.isRegularFile(path)) {
                return path;
            }
        }
        return resumeFileService.resolveForTrace(traceId);
    }

    private String extractResumeText(MultipartFile file) {
        return extractResumeText(file, detectFileType(file == null ? null : file.getOriginalFilename()), "TECH");
    }

    private String extractResumeText(MultipartFile file, String fileType, String jobCategory) {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("请上传一份 PDF、TXT、Markdown 或 CSV 简历。");
        }
        if (file.getSize() > MAX_UPLOAD_BYTES) {
            throw new IllegalArgumentException("简历文件不能超过 20MB。");
        }
        String fileName = file.getOriginalFilename() == null ? "resume" : file.getOriginalFilename();
        String lowerName = fileName.toLowerCase();
        try {
            String text;
            if (lowerName.endsWith(".pdf")) {
                try (PDDocument document = Loader.loadPDF(file.getBytes())) {
                    text = new PDFTextStripper().getText(document);
                }
            } else if (lowerName.endsWith(".txt") || lowerName.endsWith(".md") || lowerName.endsWith(".csv")) {
                text = new String(file.getBytes(), StandardCharsets.UTF_8);
            } else {
                throw new IllegalArgumentException("暂不支持该文件类型，请上传 PDF/TXT/Markdown/CSV，或将 Word 简历正文复制到文本框。");
            }
            String normalized = text == null ? "" : text.replace('\u0000', ' ').trim();
            if (!StringUtils.hasText(normalized)) {
                throw new IllegalArgumentException("未能从简历文件中抽取到有效文本，请检查文件内容或改用粘贴文本方式。");
            }
            return normalized.length() > MAX_RESUME_TEXT_LENGTH
                    ? normalized.substring(0, MAX_RESUME_TEXT_LENGTH)
                    : normalized;
        } catch (IOException e) {
            throw new IllegalArgumentException("简历文件解析失败：" + e.getMessage(), e);
        }
    }

    private void persistResumeTask(MutableTask task, String resumeObjectKey) {
        try {
            ResumeTask entity = new ResumeTask();
            entity.setId(task.id);
            entity.setTraceId(task.traceId);
            entity.setConversationId(task.conversationId);
            entity.setRevisionNo(task.revisionNo);
            entity.setWorkflowRunId(task.workflowRunId);
            entity.setBaseWorkflowRunId(task.baseWorkflowRunId);
            entity.setSupersedesTraceId(task.supersedesTraceId);
            entity.setSupersededByTraceId(task.supersededByTraceId);
            entity.setResumeText(task.resumeText);
            entity.setJobDescription(task.jobDescription);
            entity.setEvaluationBrief(task.evaluationBrief);
            entity.setInvalidatedNodes(toJson(task.invalidatedNodes));
            entity.setRagOptions(toJson(task.ragOptions));
            entity.setFileUrl(StringUtils.hasText(task.resumeFilePath) ? task.resumeFilePath : task.fileName);
            entity.setResumeObjectKey(resumeObjectKey);
            entity.setJobCategory(task.jobCategory);
            entity.setExecutionMode(task.executionMode);
            entity.setStatus(task.status);
            entity.setCandidateName(task.fileName);
            entity.setUploadedBy(task.uploadedBy);
            entity.setTenantId(task.tenantId);
            entity.setPriority(task.priority);
            entity.setQueueStatus(task.queueStatus);
            entity.setQueuedAt(task.queuedAt);
            entity.setAttemptCount(task.attemptCount);
            applyListColumns(entity, task);
            try {
                CandidateService.CandidateLink link = candidateService.upsertOnTaskCreate(
                        task.tenantId,
                        task.resumeText,
                        task.fileName,
                        task.jobCategory,
                        null,
                        task.id,
                        task.traceId);
                entity.setCandidateId(link.candidateId());
                entity.setApplicationId(link.applicationId());
                entity.setDataOrigin("USER_UPLOAD");
                entity.setCandidateLinkStatus("LINKED");
                entity.setCandidateLinkReason("UPSERT_ON_CREATE");
                if (StringUtils.hasText(link.displayName())) {
                    entity.setCandidateName(link.displayName());
                }
            } catch (Exception e) {
                log.warn("[eval] candidate upsert failed (trace={}): {}", task.traceId, e.getMessage());
            }
            if (task.topJdMatches != null && !task.topJdMatches.isEmpty()) {
                Map<String, Object> seed = new LinkedHashMap<>();
                seed.put("topJdMatches", task.topJdMatches);
                seed.put("matchedJdTitle", task.matchedJdTitle);
                seed.put("jdMatchScore", task.jdMatchScore);
                seed.put("status", task.status);
                seed.put("fileName", task.fileName);
                entity.setResultPayload(toJson(seed));
            }
            entity.setStartTime(task.createTime);
            entity.setCreateTime(task.createTime);
            entity.setUpdateTime(task.updateTime);
            resumeTaskMapper.insert(entity);
        } catch (DataAccessException e) {
            log.error("[eval] persist resume_task failed (trace={}): {}", task.traceId, e.getMessage());
            throw new IllegalStateException("评估任务持久化失败，未入队：" + task.traceId, e);
        }
    }

    private void applyListColumns(ResumeTask entity, MutableTask task) {
        entity.setFileName(task.fileName);
        entity.setOverallScore(task.overallScore);
        entity.setRecommendation(task.recommendation);
        entity.setMatchedJdTitle(task.matchedJdTitle);
        entity.setJdMatchScore(task.jdMatchScore);
        entity.setDurationMs(task.durationMs);
        entity.setTokenCost(task.tokenCost);
        entity.setSummary(trim(task.summary, 2000));
    }

    private void updateResumeTask(MutableTask task) {
        try {
            ResumeTask entity = new ResumeTask();
            entity.setId(task.id);
            entity.setStatus(task.status);
            entity.setConversationId(task.conversationId);
            entity.setRevisionNo(task.revisionNo);
            entity.setWorkflowRunId(task.workflowRunId);
            entity.setBaseWorkflowRunId(task.baseWorkflowRunId);
            entity.setSupersedesTraceId(task.supersedesTraceId);
            entity.setSupersededByTraceId(task.supersededByTraceId);
            entity.setResumeText(task.resumeText);
            entity.setJobDescription(task.jobDescription);
            entity.setEvaluationBrief(task.evaluationBrief);
            entity.setInvalidatedNodes(toJson(task.invalidatedNodes));
            entity.setRagOptions(toJson(task.ragOptions));
            entity.setEndTime(task.updateTime);
            entity.setUpdateTime(task.updateTime);
            entity.setQueueStatus(task.queueStatus);
            entity.setStartedAt(task.startedAt);
            entity.setFinishedAt(task.finishedAt);
            entity.setWorkerId(task.workerId);
            entity.setAttemptCount(task.attemptCount);
            entity.setNextRetryAt(task.nextRetryAt);
            entity.setFailReason("FAILED".equals(task.status) ? trim(task.summary, 500) : null);
            applyListColumns(entity, task);
            resumeTaskMapper.updateById(entity);
            try {
                ResumeTask linked = resumeTaskMapper.selectById(task.id);
                if (linked != null && linked.getApplicationId() != null) {
                    candidateService.syncApplicationFromTask(
                            linked.getApplicationId(),
                            task.id,
                            task.traceId,
                            task.overallScore,
                            task.recommendation);
                }
            } catch (Exception e) {
                log.debug("[eval] sync application skipped: {}", e.getMessage());
            }
            if ("RUNNING".equals(task.status)) {
                runtimeStateService.cacheRunningTask(toResponse(task));
            } else {
                runtimeStateService.evictRunningTask(task.traceId);
            }
        } catch (DataAccessException e) {
            log.error("[eval] update resume_task failed (trace={}): {}", task.traceId, e.getMessage());
            throw new IllegalStateException("评估任务状态持久化失败：" + task.traceId, e);
        }
    }

    /**
     * MySQL 分页查询任务列表，运行中任务用内存态覆盖同 traceId 行。
     */
    public PageResult<TaskListItemResponse> queryTasks(
            String keyword,
            String status,
            String recommendation,
            String jobCategory,
            String queueStatus,
            String uploadedBy,
            Integer scoreMin,
            Integer scoreMax,
            String sortBy,
            String sortOrder,
            int page,
            int pageSize) {
        PageResult<TaskListItemResponse> result = taskQueryService.queryTasks(
                keyword, status, recommendation, jobCategory, queueStatus, uploadedBy,
                scoreMin, scoreMax, sortBy, sortOrder, page, pageSize);
        List<TaskListItemResponse> merged = new ArrayList<>(result.items().size());
        for (TaskListItemResponse item : result.items()) {
            MutableTask live = tasks.get(item.traceId());
            if (live != null) {
                merged.add(overlayLiveTask(item, live));
            } else {
                merged.add(item);
            }
        }
        return PageResult.of(merged, result.total(), result.page(), result.pageSize());
    }

    private TaskListItemResponse overlayLiveTask(TaskListItemResponse base, MutableTask live) {
        return new TaskListItemResponse(
                base.id(),
                base.traceId(),
                live.fileName,
                live.jobCategory,
                live.executionMode,
                live.status,
                live.overallScore,
                live.recommendation,
                live.summary,
                live.durationMs,
                live.tokenCost,
                live.matchedJdTitle,
                live.jdMatchScore,
                base.createTime(),
                live.updateTime != null ? live.updateTime : base.updateTime(),
                buildQueueFields(live)
        );
    }

    private TaskQueueFields buildQueueFields(MutableTask task) {
        return new TaskQueueFields(
                task.queueStatus,
                task.uploadedBy,
                task.tenantId,
                task.priority,
                task.queuedAt,
                task.startedAt,
                task.finishedAt,
                task.attemptCount,
                task.nextRetryAt,
                task.workerId
        );
    }

    private TaskQueueFields buildQueueFields(ResumeTask row) {
        if (row == null) {
            return null;
        }
        return new TaskQueueFields(
                row.getQueueStatus(),
                row.getUploadedBy(),
                row.getTenantId(),
                row.getPriority(),
                row.getQueuedAt(),
                row.getStartedAt(),
                row.getFinishedAt(),
                row.getAttemptCount(),
                row.getNextRetryAt(),
                row.getWorkerId()
        );
    }

    /**
     * 删除任务及其关联数据。
     */
    public void deleteTask(String traceId) {
        loadResumeTaskRow(traceId).ifPresent(row -> {
            String status = row.getStatus();
            if (!Set.of("SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "SUPERSEDED").contains(status)) {
                cancelLinkedRun(
                        StringUtils.hasText(row.getWorkflowRunId()) ? row.getWorkflowRunId() : traceId,
                        "task_deleted", "任务被删除，取消运行");
            }
        });
        tasks.remove(traceId);
        traces.remove(traceId);
        traceSequences.remove(traceId);
        traceRoundCounters.remove(traceId);
        CompletableFuture.runAsync(() -> {
            try {
                resumeTaskMapper.delete(new LambdaQueryWrapper<ResumeTask>()
                        .eq(ResumeTask::getTraceId, traceId));
                agentExecutionTraceMapper.delete(new LambdaQueryWrapper<AgentExecutionTrace>()
                        .eq(AgentExecutionTrace::getTraceId, traceId));
                log.info("[eval] task DB records deleted: {}", traceId);
            } catch (Exception e) {
                log.warn("[eval] delete task from DB failed (trace={}): {}", traceId, e.getMessage());
            }
        });
        log.info("[eval] task deleted from memory: {}", traceId);
    }

    /**
     * 批量删除任务。
     */
    public void deleteTasks(List<String> traceIds) {
        for (String traceId : traceIds) {
            tasks.remove(traceId);
            traces.remove(traceId);
            traceSequences.remove(traceId);
            traceRoundCounters.remove(traceId);
        }
        CompletableFuture.runAsync(() -> {
            try {
                resumeTaskMapper.delete(new LambdaQueryWrapper<ResumeTask>()
                        .in(ResumeTask::getTraceId, traceIds));
                agentExecutionTraceMapper.delete(new LambdaQueryWrapper<AgentExecutionTrace>()
                        .in(AgentExecutionTrace::getTraceId, traceIds));
                log.info("[eval] batch deleted {} tasks from DB", traceIds.size());
            } catch (Exception e) {
                log.warn("[eval] batch delete from DB failed: {}", e.getMessage());
            }
        });
        log.info("[eval] batch deleted {} tasks from memory", traceIds.size());
    }

    /**
     * 查询任务详情。
     *
     * @param traceId 全局链路 ID
     * @return 任务详情
     */
    public TaskResponse getTask(String traceId) {
        MutableTask task = tasks.get(traceId);
        if (task != null) {
            return toResponse(task);
        }
        return runtimeStateService.getRunningTask(traceId)
                .orElseGet(() -> loadTaskFromDb(traceId)
                        .orElseThrow(() -> new IllegalArgumentException("任务不存在：" + traceId)));
    }

    /**
     * Returns the agent execution tree for a given task trace.
     * Uses data from AgentExecutionTrace table to build a hierarchical view.
     */
    /**
     * Server-side robust extraction of the latest agent-harness plan from trace payloads, so the
     * frontend gets a clean top-level object instead of fragile client-side brace matching.
     */
    private Map<String, Object> extractHarnessPlan(List<AgentExecutionTrace> traces) {
        com.fasterxml.jackson.databind.JsonNode found = null;
        for (AgentExecutionTrace t : traces) {
            String payload = t.getPayload();
            if (payload == null || !payload.contains("agent-harness-v1")) {
                continue;
            }
            try {
                com.fasterxml.jackson.databind.JsonNode plan = findHarnessPlanNode(objectMapper.readTree(payload));
                if (plan != null) {
                    found = plan; // keep the latest occurrence in execution order
                }
            } catch (Exception ignored) {
            }
        }
        if (found == null) {
            return null;
        }
        try {
            return objectMapper.convertValue(found, Map.class);
        } catch (Exception e) {
            return null;
        }
    }

    private com.fasterxml.jackson.databind.JsonNode findHarnessPlanNode(com.fasterxml.jackson.databind.JsonNode node) {
        if (node == null) {
            return null;
        }
        if (node.isObject()) {
            com.fasterxml.jackson.databind.JsonNode version = node.get("version");
            if (version != null && "agent-harness-v1".equals(version.asText()) && node.has("route")) {
                return node;
            }
            java.util.Iterator<Map.Entry<String, com.fasterxml.jackson.databind.JsonNode>> it = node.fields();
            while (it.hasNext()) {
                com.fasterxml.jackson.databind.JsonNode child = it.next().getValue();
                if (child.isTextual()) {
                    String text = child.asText();
                    if (text.contains("agent-harness-v1")) {
                        try {
                            com.fasterxml.jackson.databind.JsonNode parsed = findHarnessPlanNode(objectMapper.readTree(text));
                            if (parsed != null) {
                                return parsed;
                            }
                        } catch (Exception ignored) {
                        }
                    }
                } else {
                    com.fasterxml.jackson.databind.JsonNode parsed = findHarnessPlanNode(child);
                    if (parsed != null) {
                        return parsed;
                    }
                }
            }
        } else if (node.isArray()) {
            for (com.fasterxml.jackson.databind.JsonNode child : node) {
                com.fasterxml.jackson.databind.JsonNode parsed = findHarnessPlanNode(child);
                if (parsed != null) {
                    return parsed;
                }
            }
        }
        return null;
    }

    public Map<String, Object> getAgentExecutionTree(String traceId) {
        List<AgentExecutionTrace> traces = agentExecutionTraceMapper.selectList(
                new LambdaQueryWrapper<AgentExecutionTrace>()
                        .eq(AgentExecutionTrace::getTraceId, traceId)
                        .orderByAsc(AgentExecutionTrace::getCreateTime));

        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("traceId", traceId);
        tree.put("framework", "Unified Agent Runtime + DeepSeek");
        tree.put("architecture", "6-Agent LangGraph Orchestration");

        List<AgentExecutionTrace> deduped = dedupeTracesByEventId(traces);
        boolean hasLangGraphEvents = deduped.stream().anyMatch(t -> StringUtils.hasText(t.getEventId()));

        Map<String, List<AgentExecutionTrace>> groupedByNode = new LinkedHashMap<>();
        Set<String> allowedAgents = Set.of(
                "CoordinatorAgent", "TechAgent", "ProjectAgent",
                "RiskAgent", "EvidenceAgent", "ReportAgent");
        for (AgentExecutionTrace t : deduped) {
            if (!allowedAgents.contains(t.getAgentRole())) {
                continue;
            }
            String groupKey = hasLangGraphEvents && StringUtils.hasText(t.getNodeId())
                    ? t.getNodeId()
                    : t.getAgentRole();
            groupedByNode.computeIfAbsent(groupKey, k -> new ArrayList<>()).add(t);
        }

        List<Map<String, Object>> executionTree = new ArrayList<>();
        for (Map.Entry<String, List<AgentExecutionTrace>> entry : groupedByNode.entrySet()) {
            List<AgentExecutionTrace> nodeTraces = entry.getValue();
            if (nodeTraces.isEmpty()) {
                continue;
            }
            AgentExecutionTrace representative = nodeTraces.get(nodeTraces.size() - 1);
            String agentName = representative.getAgentRole();

            Map<String, Object> agentNode = new LinkedHashMap<>();
            agentNode.put("name", agentName);
            agentNode.put("role", mapAgentDescription(agentName));
            agentNode.put("phase", mapAgentPhase(agentName));
            agentNode.put("nodeId", representative.getNodeId());

            String agentStatus = "SUCCESS";
            AgentExecutionTrace nodeEnd = nodeTraces.stream()
                    .filter(t -> "node".equals(t.getEventKind()) && "node_end".equals(t.getRoundRole()))
                    .findFirst()
                    .orElse(null);
            AgentExecutionTrace nodeStart = nodeTraces.stream()
                    .filter(t -> "node".equals(t.getEventKind()) && "node_start".equals(t.getRoundRole()))
                    .findFirst()
                    .orElse(null);
            long totalDuration = nodeEnd != null && nodeEnd.getDurationMs() != null ? nodeEnd.getDurationMs() : 0L;
            for (AgentExecutionTrace t : nodeTraces) {
                if (totalDuration <= 0 && t.getDurationMs() != null) {
                    totalDuration += t.getDurationMs();
                }
                if ("FAILED".equals(t.getStatus())) {
                    agentStatus = "FAILED";
                }
            }
            agentNode.put("status", agentStatus);
            agentNode.put("durationMs", totalDuration);
            if (nodeStart != null && nodeStart.getStartedAt() != null) {
                agentNode.put("startedAt", nodeStart.getStartedAt());
            } else if (nodeEnd != null && nodeEnd.getStartedAt() != null) {
                agentNode.put("startedAt", nodeEnd.getStartedAt());
            }
            if (nodeEnd != null && nodeEnd.getEndedAt() != null) {
                agentNode.put("endedAt", nodeEnd.getEndedAt());
            }

            List<Map<String, Object>> rounds = hasLangGraphEvents
                    ? buildLangGraphRounds(nodeTraces, deduped)
                    : buildRoundsFromTraces(nodeTraces);
            agentNode.put("rounds", rounds);
            agentNode.put("totalRounds", rounds.size());
            agentNode.put("output", representative.getOutputSummary());
            agentNode.put("spanId", representative.getSpanId());
            executionTree.add(agentNode);
        }

        // No fabricated placeholder tree: an empty result means the caller
        // (TaskController) falls back to the unified run-event bridge.
        tree.put("executionTree", executionTree);
        tree.put("harnessPlan", extractHarnessPlan(deduped));
        return tree;
    }

    private List<AgentExecutionTrace> dedupeTracesByEventId(List<AgentExecutionTrace> traces) {
        Map<String, AgentExecutionTrace> latest = new LinkedHashMap<>();
        for (AgentExecutionTrace trace : traces) {
            if (!StringUtils.hasText(trace.getEventId())) {
                continue;
            }
            AgentExecutionTrace existing = latest.get(trace.getEventId());
            if (existing == null
                    || (trace.getUpdateTime() != null
                    && existing.getUpdateTime() != null
                    && trace.getUpdateTime().isAfter(existing.getUpdateTime()))) {
                latest.put(trace.getEventId(), trace);
            }
        }
        List<AgentExecutionTrace> withoutId = traces.stream()
                .filter(t -> !StringUtils.hasText(t.getEventId()))
                .toList();
        List<AgentExecutionTrace> merged = new ArrayList<>(latest.values());
        merged.addAll(withoutId);
        merged.sort(Comparator.comparing(AgentExecutionTrace::getCreateTime, Comparator.nullsLast(Comparator.naturalOrder())));
        return merged;
    }

    private List<Map<String, Object>> buildLangGraphRounds(List<AgentExecutionTrace> nodeTraces,
                                                           List<AgentExecutionTrace> allTraces) {
        List<AgentExecutionTrace> generations = nodeTraces.stream()
                .filter(t -> "generation".equals(t.getEventKind()))
                .sorted(Comparator.comparing(
                        t -> t.getRoundIndex() != null ? t.getRoundIndex() : 0))
                .toList();

        Map<String, AgentExecutionTrace> finalsByParent = new LinkedHashMap<>();
        for (AgentExecutionTrace t : nodeTraces) {
            if ("final".equals(t.getEventKind()) && StringUtils.hasText(t.getParentSpanId())) {
                finalsByParent.put(t.getParentSpanId(), t);
            }
        }

        Map<String, List<AgentExecutionTrace>> toolsByParent = new LinkedHashMap<>();
        Set<String> generationEventIds = generations.stream()
                .map(AgentExecutionTrace::getEventId)
                .filter(StringUtils::hasText)
                .collect(java.util.stream.Collectors.toCollection(LinkedHashSet::new));
        String currentNodeId = nodeTraces.isEmpty() ? null : nodeTraces.get(0).getNodeId();
        List<Map<String, Object>> orphanToolCalls = new ArrayList<>();
        for (AgentExecutionTrace t : allTraces) {
            if (!"tool".equals(t.getEventKind())) {
                continue;
            }
            if (!Objects.equals(t.getNodeId(), currentNodeId)) {
                continue;
            }
            String parent = extractParentEventId(t);
            if (parent != null && generationEventIds.contains(parent)) {
                toolsByParent.computeIfAbsent(parent, k -> new ArrayList<>()).add(t);
            } else {
                Integer toolRound = t.getRoundIndex();
                AgentExecutionTrace matchedGen = generations.stream()
                        .filter(g -> Objects.equals(g.getRoundIndex(), toolRound))
                        .findFirst()
                        .orElse(null);
                if (matchedGen == null && !generations.isEmpty()) {
                    matchedGen = generations.stream()
                            .filter(g -> g.getRoundIndex() != null && g.getRoundIndex() >= (toolRound != null ? toolRound : 0))
                            .findFirst()
                            .orElse(generations.get(generations.size() - 1));
                }
                if (matchedGen != null) {
                    toolsByParent.computeIfAbsent(matchedGen.getEventId(), k -> new ArrayList<>()).add(t);
                } else {
                    orphanToolCalls.addAll(parseToolCallsFromPayload(t));
                }
            }
        }

        List<Map<String, Object>> rounds = new ArrayList<>();
        for (AgentExecutionTrace gen : generations) {
            Map<String, Object> round = new LinkedHashMap<>();
            int roundNum = gen.getRoundIndex() != null ? gen.getRoundIndex() : rounds.size() + 1;
            String eventId = gen.getEventId();
            round.put("id", eventId);
            round.put("eventId", eventId);
            round.put("nodeId", gen.getNodeId());
            round.put("roundNum", roundNum);
            round.put("type", "generation");
            round.put("status", gen.getStatus());
            round.put("input", gen.getInputSummary());
            round.put("output", gen.getOutputSummary());
            round.put("tokens", gen.getCostTokens() != null ? gen.getCostTokens() : 0);
            round.put("inputMessages", parseInputMessages(gen));
            round.put("outputMessage", parseOutputMessage(gen));

            Map<String, Object> payload = parsePayloadMap(gen);
            boolean hasToolCalls = boolValue(payload.get("hasToolCalls"));
            String decisionText = stringValue(payload.get("decisionText"), null);
            String finalOutput = stringValue(payload.get("finalOutput"), null);
            String roundRole = stringValue(payload.get("roundRole"), null);
            if (!StringUtils.hasText(decisionText) && hasToolCalls) {
                decisionText = gen.getOutputSummary();
            }

            List<Map<String, Object>> toolCalls = new ArrayList<>();
            LinkedHashMap<String, Map<String, Object>> toolCallMap = new LinkedHashMap<>();
            List<AgentExecutionTrace> parentTools = toolsByParent.getOrDefault(eventId, List.of());
            for (AgentExecutionTrace toolTrace : parentTools) {
                for (Map<String, Object> tc : parseToolCallsFromPayload(toolTrace)) {
                    mergeToolCall(toolCallMap, tc);
                }
            }

            round.put("hasToolCalls", hasToolCalls || !toolCallMap.isEmpty());
            round.put("decisionText", decisionText);
            round.put("roundRole", roundRole);
            round.put("toolCalls", new ArrayList<>(toolCallMap.values()));

            AgentExecutionTrace finalEvent = finalsByParent.get(eventId);
            if (finalEvent != null) {
                round.put("final", true);
                String fo = stringValue(parsePayloadMap(finalEvent).get("finalOutput"), finalEvent.getOutputSummary());
                round.put("finalOutput", fo);
                if (StringUtils.hasText(fo)) {
                    round.put("output", fo);
                }
            } else if ("final".equals(roundRole) || Boolean.FALSE.equals(payload.get("hasToolCalls"))) {
                round.put("final", true);
                round.put("finalOutput", finalOutput != null ? finalOutput : gen.getOutputSummary());
            } else {
                round.put("final", false);
            }
            rounds.add(round);
        }

        return rounds;
    }

    private Map<String, Object> parsePayloadMap(AgentExecutionTrace trace) {
        if (!StringUtils.hasText(trace.getPayload())) {
            return Map.of();
        }
        try {
            return objectMapper.readValue(trace.getPayload(), new TypeReference<>() {});
        } catch (Exception ignored) {
            return Map.of();
        }
    }

    private String extractParentEventId(AgentExecutionTrace trace) {
        if (StringUtils.hasText(trace.getParentEventId())) {
            return trace.getParentEventId();
        }
        if (trace.getPayload() != null) {
            try {
                Map<String, Object> payload = objectMapper.readValue(trace.getPayload(), new TypeReference<>() {});
                Object parent = payload.get("parentEventId");
                if (parent != null) {
                    return String.valueOf(parent);
                }
            } catch (Exception ignored) {
            }
        }
        return trace.getParentSpanId();
    }

    private List<Map<String, Object>> parseInputMessages(AgentExecutionTrace trace) {
        if (StringUtils.hasText(trace.getRawInput())) {
            try {
                Object parsed = objectMapper.readValue(trace.getRawInput(), Object.class);
                if (parsed instanceof List<?> list) {
                    return list.stream().map(item -> (Map<String, Object>) item).toList();
                }
            } catch (Exception ignored) {
            }
        }
        if (trace.getPayload() != null) {
            try {
                Map<String, Object> payload = objectMapper.readValue(trace.getPayload(), new TypeReference<>() {});
                Object msgs = payload.get("inputMessages");
                if (msgs instanceof List<?> list) {
                    return list.stream().map(item -> (Map<String, Object>) item).toList();
                }
            } catch (Exception ignored) {
            }
        }
        return List.of();
    }

    private Map<String, Object> parseOutputMessage(AgentExecutionTrace trace) {
        if (StringUtils.hasText(trace.getRawOutput())) {
            try {
                return objectMapper.readValue(trace.getRawOutput(), new TypeReference<>() {});
            } catch (Exception ignored) {
            }
        }
        if (trace.getPayload() != null) {
            try {
                Map<String, Object> payload = objectMapper.readValue(trace.getPayload(), new TypeReference<>() {});
                Object out = payload.get("outputMessage");
                if (out instanceof Map<?, ?> map) {
                    return (Map<String, Object>) map;
                }
            } catch (Exception ignored) {
            }
        }
        return Map.of();
    }

    private List<Map<String, Object>> buildRoundsFromTraces(List<AgentExecutionTrace> agentTraces) {
        List<Map<String, Object>> rounds = new ArrayList<>();
        int roundNum = 0;
        for (AgentExecutionTrace t : agentTraces) {
            roundNum++;
            Map<String, Object> round = new LinkedHashMap<>();
            round.put("roundNum", roundNum);
            String eventType = t.getToolCall();
            round.put("type", "LLM_TOOL_CALL".equals(eventType) ? "tool_call" : "generation");
            round.put("input", t.getInputSummary());
            round.put("output", t.getOutputSummary());
            round.put("tokens", t.getCostTokens() != null ? t.getCostTokens() : 0);

            List<Map<String, Object>> toolCalls = parseToolCallsFromPayload(t);
            round.put("toolCalls", toolCalls);
            rounds.add(round);
        }
        return rounds;
    }

    private List<Map<String, Object>> parseToolCallsFromPayload(AgentExecutionTrace t) {
        List<Map<String, Object>> toolCalls = new ArrayList<>();
        if (t.getPayload() != null) {
            try {
                Map<String, Object> payload = objectMapper.readValue(t.getPayload(), new TypeReference<>() {});
                Object tcRaw = payload.get("toolCalls");
                Object mcpRaw = payload.get("mcpCalls");
                if (tcRaw instanceof List<?> tcList) {
                    for (Object item : tcList) {
                        if (item instanceof Map<?, ?> rawMap) {
                            Map<String, Object> mapItem = objectMapper.convertValue(rawMap, new TypeReference<Map<String, Object>>() {});
                            Map<String, Object> tc = new LinkedHashMap<>();
                            tc.put("name", stringValue(mapItem.get("name"), "unknown"));
                            tc.put("toolCallId", stringValue(mapItem.get("toolCallId"), stringValue(mapItem.get("id"), "")));
                            tc.put("category", stringValue(mapItem.get("type"), stringValue(mapItem.get("category"), stringValue(mapItem.get("family"), "tool"))));
                            tc.put("origin", stringValue(mapItem.get("origin"), null));
                            tc.put("family", stringValue(mapItem.get("family"), stringValue(tc.get("category"), "tool")));
                            tc.put("protocol", stringValue(mapItem.get("protocol"), null));
                            tc.put("server", stringValue(mapItem.get("server"), null));
                            tc.put("operation", stringValue(mapItem.get("operation"), null));
                            tc.put("input", stringValue(mapItem.get("arguments"), stringValue(mapItem.get("input"), "")));
                            tc.put("output", stringValue(mapItem.get("result"), stringValue(mapItem.get("output"), "")));
                            tc.put("durationMs", intValue(mapItem.get("durationMs"), 0));
                            tc.put("status", stringValue(mapItem.get("status"), t.getStatus() != null ? t.getStatus() : "SUCCESS"));
                            tc.put("startedAt", stringValue(mapItem.get("startedAt"), null));
                            tc.put("endedAt", stringValue(mapItem.get("endedAt"), null));
                            tc.put("inputHash", stringValue(mapItem.get("inputHash"), null));
                            tc.put("dedupedCount", intValue(mapItem.get("dedupedCount"), 0));
                            tc.put("substeps", mapItem.get("substeps"));
                            tc.put("retrieval", mapItem.get("retrieval"));
                            toolCalls.add(tc);
                        } else {
                            toolCalls.add(parseToolEntry(item.toString(), "tool"));
                        }
                    }
                }
                if (mcpRaw instanceof List<?> mcpList) {
                    for (Object item : mcpList) {
                        toolCalls.add(parseToolEntry(item.toString(), "mcp"));
                    }
                }
            } catch (Exception ignored) {}
        }
        if (toolCalls.isEmpty() && t.getToolCall() != null && !"AGENT_EXECUTION".equals(t.getToolCall())
                && !"LLM_GENERATION".equals(t.getToolCall())) {
            Map<String, Object> tc = new LinkedHashMap<>();
            tc.put("name", t.getToolCall());
            tc.put("category", categorizeToolCall(t.getToolCall()));
            tc.put("input", "");
            tc.put("output", "");
            tc.put("durationMs", 0);
            tc.put("status", t.getStatus() != null ? t.getStatus() : "SUCCESS");
            toolCalls.add(tc);
        }
        return toolCalls;
    }

    private void mergeToolCall(LinkedHashMap<String, Map<String, Object>> toolCallMap, Map<String, Object> tc) {
        String name = stringValue(tc.get("name"), "unknown");
        String inputHash = stringValue(tc.get("inputHash"), "");
        String input = stringValue(tc.get("input"), "");
        String key = StringUtils.hasText(inputHash)
                ? name + ":" + inputHash
                : name + ":" + Integer.toUnsignedString(input.hashCode());

        Map<String, Object> existing = toolCallMap.get(key);
        if (existing == null) {
            toolCallMap.put(key, tc);
            return;
        }
        int deduped = intValue(existing.get("dedupedCount"), 0) + 1;
        existing.put("dedupedCount", deduped);
    }

    private Map<String, Object> parseToolEntry(String entry, String defaultCategory) {
        Map<String, Object> tc = new LinkedHashMap<>();
        // Try JSON format first: {"name":"...", "type":"...", "arguments":"...", "result":"..."}
        if (entry.startsWith("{")) {
            try {
                Map<String, Object> jsonEntry = objectMapper.readValue(entry, new TypeReference<>() {});
                tc.put("name", jsonEntry.getOrDefault("name", "unknown"));
                tc.put("toolCallId", jsonEntry.getOrDefault("toolCallId", jsonEntry.get("id")));
                tc.put("category", jsonEntry.getOrDefault("type", defaultCategory));
                tc.put("input", jsonEntry.getOrDefault("arguments", ""));
                tc.put("output", jsonEntry.getOrDefault("result", ""));
                tc.put("durationMs", jsonEntry.getOrDefault("durationMs", 0));
                tc.put("status", jsonEntry.getOrDefault("status", "SUCCESS"));
                return tc;
            } catch (Exception ignored) {}
        }
        // Legacy format: name(args)→result
        int parenIdx = entry.indexOf('(');
        int arrowIdx = entry.indexOf("→");
        if (parenIdx > 0 && arrowIdx > parenIdx) {
            tc.put("name", entry.substring(0, parenIdx));
            tc.put("category", defaultCategory);
            int closeParenIdx = entry.lastIndexOf(')', arrowIdx);
            tc.put("input", closeParenIdx > parenIdx ? entry.substring(parenIdx + 1, closeParenIdx) : "");
            tc.put("output", entry.substring(arrowIdx + 1).trim());
        } else {
            tc.put("name", entry);
            tc.put("category", defaultCategory);
            tc.put("input", "");
            tc.put("output", "");
        }
        tc.put("durationMs", 0);
        return tc;
    }

    private String categorizeToolCall(String toolName) {
        if (toolName == null) return "tool";
        if (toolName.contains("mcp_") || toolName.contains("github") || toolName.contains("blog") || toolName.contains("stackoverflow")) return "mcp";
        if (toolName.contains("skill") || toolName.contains("execute_skill")) return "skill";
        return "tool";
    }

    private int mapAgentPhase(String agentRole) {
        if (agentRole == null) return 1;
        if (agentRole.contains("Intent")) return 1;
        if (agentRole.contains("Parse") || agentRole.contains("Resume")) return 2;
        if (agentRole.contains("JdMatch") || agentRole.contains("Jd")) return 3;
        if (agentRole.contains("Tech") || agentRole.contains("Project") || agentRole.contains("Risk")) return 4;
        if (agentRole.contains("Fusion") || agentRole.contains("Evidence")) return 5;
        if (agentRole.contains("Report")) return 6;
        return 1;
    }

    private String mapAgentDescription(String agentRole) {
        if (agentRole == null) return "";
        if (agentRole.contains("Intent")) return "意图路由";
        if (agentRole.contains("Parse")) return "简历结构化解析";
        if (agentRole.contains("JdMatch")) return "岗位匹配";
        if (agentRole.contains("TechEval")) return "技术评估";
        if (agentRole.contains("ProjectEval")) return "项目深度评估";
        if (agentRole.contains("Risk")) return "风险识别";
        if (agentRole.contains("Fusion") || agentRole.contains("Evidence")) return "证据融合";
        if (agentRole.contains("Report")) return "报告生成";
        return agentRole;
    }

    private Optional<TaskResponse> loadTaskFromDb(String traceId) {
        try {
            ResumeTask row = resumeTaskMapper.selectOne(
                    new QueryWrapper<ResumeTask>().eq("trace_id", traceId).last("limit 1"));
            if (row == null) {
                return Optional.empty();
            }
            if (StringUtils.hasText(row.getResultPayload())) {
                Map<String, Object> payload = objectMapper.readValue(row.getResultPayload(), new TypeReference<>() {});
                return Optional.of(toResponse(hydrateTaskFromPayload(row, payload)));
            }
            MutableTask stub = new MutableTask(
                    row.getId(), row.getTraceId(),
                    StringUtils.hasText(row.getFileName()) ? row.getFileName() : row.getCandidateName(),
                    row.getJobCategory(), row.getExecutionMode(), row.getStatus(),
                    row.getOverallScore(), row.getRecommendation(), row.getSummary(),
                    row.getDurationMs() != null ? row.getDurationMs() : 0L,
                    row.getTokenCost() != null ? row.getTokenCost() : 0,
                    row.getCreateTime(), row.getUpdateTime(),
                    List.of(), List.of(), List.of(), null,
                    row.getResumeText(),
                    StringUtils.hasText(row.getResumeObjectKey()) || StringUtils.hasText(row.getFileUrl())
                            ? row.getFileUrl() : null,
                    null, row.getMatchedJdTitle(), row.getJdMatchScore(), List.of(),
                    null, null, null);
            applyQueueFieldsFromRow(stub, row);
            applyRevisionFieldsFromRow(stub, row);
            applyCandidateFieldsFromRow(stub, row);
            return Optional.of(toResponse(stub));
        } catch (Exception e) {
            log.warn("[eval] load task from db failed (trace={}): {}", traceId, e.getMessage());
            return Optional.empty();
        }
    }

    /**
     * 查询 Trace 事件。内存优先，缺失时回退查询 MySQL，保证刷新页面后仍能拉到完整历史链路。
     *
     * @param traceId 全局链路 ID
     * @return Trace 事件列表
     */
    public List<TraceEventResponse> listTraces(String traceId) {
        List<TraceEventResponse> events = enrichTraceRoundSemantics(filterLegacyTraceDuplicates(loadTraceEvents(traceId)));
        return enrichWithExpectedDagNodes(traceId, events);
    }

    private List<TraceEventResponse> loadTraceEvents(String traceId) {
        List<TraceEventResponse> inMemory = traces.get(traceId);
        if (inMemory != null && !inMemory.isEmpty()) {
            return new ArrayList<>(inMemory);
        }
        try {
            QueryWrapper<AgentExecutionTrace> wrapper = new QueryWrapper<>();
            wrapper.eq("trace_id", traceId).orderByAsc("create_time", "id");
            List<AgentExecutionTrace> rows = agentExecutionTraceMapper.selectList(wrapper);
            List<TraceEventResponse> result = new ArrayList<>(rows.size());
            for (AgentExecutionTrace row : rows) {
                result.add(fromPersistedTrace(row));
            }
            if (!result.isEmpty()) {
                traces.put(traceId, new ArrayList<>(result));
            }
            return result;
        } catch (DataAccessException e) {
            log.warn("[eval] load trace from db failed (trace={}): {}", traceId, e.getMessage());
            return List.of();
        }
    }

    private TraceEventResponse fromPersistedTrace(AgentExecutionTrace row) {
        Map<String, Object> payload = parseTracePayload(row.getPayload());
        if (payload != null && (payload.containsKey("stepKind")
                || payload.containsKey("kind")
                || payload.containsKey("eventId"))) {
            return buildTraceFromPayload(row, payload);
        }
        return new TraceEventResponse(
                row.getTraceId(),
                row.getSpanId(),
                row.getParentSpanId(),
                row.getAgentRole(),
                row.getToolCall(),
                row.getInputSummary(),
                row.getOutputSummary(),
                row.getStatus(),
                row.getDurationMs(),
                row.getCostTokens() == null ? 0 : row.getCostTokens().intValue(),
                row.getCreateTime()
        );
    }

    private Map<String, Object> parseTracePayload(String payloadJson) {
        if (!StringUtils.hasText(payloadJson)) {
            return null;
        }
        try {
            return objectMapper.readValue(payloadJson, new TypeReference<>() {
            });
        } catch (Exception e) {
            log.warn("[eval] parse trace payload failed: {}", e.getMessage());
            return null;
        }
    }

    @SuppressWarnings("unchecked")
    private TraceEventResponse buildTraceFromPayload(AgentExecutionTrace row, Map<String, Object> payload) {
        return new TraceEventResponse(
                row.getTraceId(),
                row.getSpanId(),
                row.getParentSpanId(),
                stringValue(payload.get("agentRole"), row.getAgentRole()),
                stringValue(payload.get("eventType"), stringValue(payload.get("kind"), row.getToolCall())),
                stringValue(payload.get("title"), row.getAgentRole() + " " + stringValue(row.getEventKind(), "")),
                stringValue(payload.get("detail"), row.getOutputSummary()),
                stringValue(payload.get("status"), row.getStatus()),
                row.getDurationMs(),
                row.getCostTokens() == null ? 0 : row.getCostTokens().intValue(),
                row.getCreateTime(),
                stringValue(payload.get("dagGroupId"), null),
                stringValue(payload.get("laneId"), null),
                stringValue(payload.get("stepKind"), null),
                stringValue(payload.get("viewType"), "BOTH"),
                stringValue(payload.get("businessLabel"), null),
                stringValue(payload.get("evidenceSummary"), null),
                (List<String>) payload.get("interviewHints"),
                stringValue(payload.get("developerLabel"), null),
                stringValue(payload.get("skillName"), row.getSkillName()),
                stringValue(payload.get("promptPreview"), null),
                stringValue(payload.get("inputSummary"), row.getInputSummary()),
                stringValue(payload.get("outputSummary"), row.getOutputSummary()),
                (List<String>) payload.get("toolCalls"),
                (List<String>) payload.get("mcpCalls"),
                stringValue(payload.get("executionSummary"), null),
                stringValue(payload.get("llmInvocationId"), null),
                stringValue(payload.get("nodeId"), row.getNodeId()),
                (List<String>) payload.get("dependsOn"),
                stringValue(payload.get("edgeLabel"), null),
                stringValue(payload.get("phase"), null),
                boolValue(payload.get("expected")),
                payload.get("sortOrder") == null ? null : intValue(payload.get("sortOrder"), 0),
                stringValue(payload.get("fullPrompt"), null),
                stringValue(payload.get("fullInput"), null),
                stringValue(payload.get("fullOutput"), null),
                payload.get("sequence") == null ? null : intValue(payload.get("sequence"), 0),
                payload.get("roundIndex") == null
                        ? row.getRoundIndex()
                        : intValue(payload.get("roundIndex"), row.getRoundIndex() != null ? row.getRoundIndex() : 0),
                stringValue(payload.get("roundRole"), row.getRoundRole()),
                stringValue(payload.get("callKind"), row.getCallKind()),
                stringValue(payload.get("callName"), row.getCallName()),
                stringValue(payload.get("parentAgentSpanId"), null),
                stringValue(payload.get("parentRoundId"), row.getParentRoundId()),
                buildIoJson(row, payload)
        );
    }

    private String buildIoJson(AgentExecutionTrace row, Map<String, Object> payload) {
        Object ioJson = payload.get("ioJson");
        if (ioJson != null) {
            return String.valueOf(ioJson);
        }
        if (StringUtils.hasText(row.getRawInput()) || StringUtils.hasText(row.getRawOutput())) {
            try {
                Map<String, Object> io = new LinkedHashMap<>();
                if (StringUtils.hasText(row.getRawInput())) {
                    io.put("inputMessages", objectMapper.readValue(row.getRawInput(), Object.class));
                }
                if (StringUtils.hasText(row.getRawOutput())) {
                    io.put("outputMessage", objectMapper.readValue(row.getRawOutput(), Object.class));
                }
                return objectMapper.writeValueAsString(io);
            } catch (Exception ignored) {
            }
        }
        return null;
    }

    private List<TraceEventResponse> enrichWithExpectedDagNodes(String traceId, List<TraceEventResponse> events) {
        if (events == null || events.isEmpty()) {
            return List.of();
        }
        boolean hasDag = events.stream().anyMatch(e ->
                StringUtils.hasText(e.stepKind()) || Boolean.TRUE.equals(e.expected()));
        if (!hasDag) {
            return events;
        }
        String status = resolveTaskStatus(traceId);
        if ("SUCCESS".equals(status) || "FAILED".equals(status)) {
            return sortTraceEvents(events);
        }
        boolean includeUpload = events.stream().anyMatch(e -> "upload_parse".equals(e.stepKind()));
        Set<String> presentNodeIds = new HashSet<>();
        for (TraceEventResponse event : events) {
            if (Boolean.TRUE.equals(event.expected())) {
                continue;
            }
            if (StringUtils.hasText(event.nodeId())) {
                presentNodeIds.add(event.nodeId());
            } else if (StringUtils.hasText(event.stepKind())) {
                presentNodeIds.add(DagStepRegistry.resolveNodeId(event.stepKind(), event.laneId()));
            }
        }
        List<TraceEventResponse> merged = new ArrayList<>(events.stream()
                .filter(e -> !Boolean.TRUE.equals(e.expected()))
                .toList());
        for (StepDefinition definition : DagStepRegistry.skeleton(includeUpload)) {
            if (!presentNodeIds.contains(definition.nodeId())) {
                merged.add(syntheticPendingNode(traceId, definition));
            }
        }
        return sortTraceEvents(merged);
    }

    private TraceEventResponse syntheticPendingNode(String traceId, StepDefinition definition) {
        String dagGroupId = definition.laneId() != null ? "parallel-evaluation" : null;
        return new TraceEventResponse(
                traceId,
                "expected-" + definition.nodeId(),
                null,
                "DAGEngine",
                "EXPECTED_NODE",
                definition.businessLabel(),
                "等待上游节点完成后执行",
                "PENDING",
                0L,
                0,
                LocalDateTime.now(),
                dagGroupId,
                definition.laneId(),
                definition.stepKind(),
                definition.viewType(),
                definition.businessLabel(),
                "等待中",
                null,
                definition.developerLabel(),
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                definition.nodeId(),
                definition.dependsOn(),
                definition.edgeLabel(),
                definition.phase(),
                true,
                definition.sortOrder(),
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null
        );
    }

    private List<TraceEventResponse> sortTraceEvents(List<TraceEventResponse> events) {
        return events.stream()
                .sorted(Comparator
                        .comparing((TraceEventResponse e) -> e.sortOrder() == null ? 999 : e.sortOrder())
                        .thenComparing(e -> e.sequence() == null ? Integer.MAX_VALUE : e.sequence())
                        .thenComparing(e -> e.timestamp() == null ? LocalDateTime.MIN : e.timestamp()))
                .toList();
    }

    private int nextSequence(String traceId) {
        return traceSequences.computeIfAbsent(traceId, ignored -> new AtomicInteger(0)).incrementAndGet();
    }

    private int nextRoundIndex(String traceId, String nodeId) {
        if (!StringUtils.hasText(nodeId)) {
            return 1;
        }
        return traceRoundCounters
                .computeIfAbsent(traceId, ignored -> new ConcurrentHashMap<>())
                .computeIfAbsent(nodeId, ignored -> new AtomicInteger(0))
                .incrementAndGet();
    }

    private String inferRoundRole(String stepKind, String callKind) {
        if ("llm".equals(callKind) || "llm_complete".equals(stepKind)) {
            return "llm";
        }
        return "attempt";
    }

    private String inferCallKind(String stepKind, List<String> toolCalls,
                                 List<String> mcpCalls, String llmInvocationId) {
        if (StringUtils.hasText(llmInvocationId)) {
            return "llm";
        }
        if (mcpCalls != null && !mcpCalls.isEmpty()) {
            return "mcp";
        }
        if (stepKind != null && (stepKind.contains("rag") || "jd_match".equals(stepKind)
                || "historical_match".equals(stepKind))) {
            return "rag";
        }
        List<String> realTools = sanitizeToolCalls(toolCalls);
        if (!realTools.isEmpty()) {
            return "tool";
        }
        return "none";
    }

    private String inferCallName(String stepKind, List<String> toolCalls, List<String> mcpCalls, String llmInvocationId) {
        if (StringUtils.hasText(llmInvocationId)) {
            return "DeepSeekChatModel";
        }
        if (mcpCalls != null && !mcpCalls.isEmpty()) {
            return extractCallName(mcpCalls.get(0));
        }
        List<String> realTools = sanitizeToolCalls(toolCalls);
        if (!realTools.isEmpty()) {
            return extractCallName(realTools.get(0));
        }
        if ("jd_match".equals(stepKind)) {
            return "milvus.search";
        }
        if ("historical_match".equals(stepKind)) {
            return "milvus.search";
        }
        if ("rag_retrieve".equals(stepKind)) {
            return "milvus.search";
        }
        if ("report_generate".equals(stepKind)) {
            return "ReportAssembly";
        }
        return stepKind;
    }

    private String extractCallName(String raw) {
        if (!StringUtils.hasText(raw)) {
            return raw;
        }
        try {
            Map<String, Object> parsed = objectMapper.readValue(raw, new TypeReference<>() {
            });
            Object name = parsed.get("name");
            if (name != null && StringUtils.hasText(name.toString())) {
                return name.toString();
            }
            Object server = parsed.get("server");
            if (server != null && StringUtils.hasText(server.toString())) {
                return server.toString();
            }
        } catch (Exception ignored) {
            // fall through to raw string
        }
        return raw;
    }

    private List<String> sanitizeToolCalls(List<String> toolCalls) {
        if (toolCalls == null || toolCalls.isEmpty()) {
            return List.of();
        }
        List<String> sanitized = new ArrayList<>();
        for (String raw : toolCalls) {
            if (isPseudoToolEntry(raw)) {
                continue;
            }
            sanitized.add(raw);
        }
        return sanitized;
    }

    private boolean isPseudoToolEntry(String raw) {
        if (!StringUtils.hasText(raw)) {
            return true;
        }
        String name = extractCallName(raw);
        if (StringUtils.hasText(name)) {
            if (name.endsWith("Skill")) {
                return true;
            }
            if ("deepseek-chat".equalsIgnoreCase(name)) {
                return true;
            }
            if (name.startsWith("{") && name.contains("Skill")) {
                return true;
            }
        }
        return raw.contains("\"name\"") && raw.contains("Skill\"");
    }

    private List<TraceEventResponse> enrichTraceRoundSemantics(List<TraceEventResponse> events) {
        if (events == null || events.isEmpty()) {
            return List.of();
        }
        Map<String, Integer> roundCounters = new LinkedHashMap<>();
        List<TraceEventResponse> sorted = sortTraceEvents(events);
        List<TraceEventResponse> enriched = new ArrayList<>(sorted.size());
        for (TraceEventResponse event : sorted) {
            String nodeId = event.nodeId();
            if (!StringUtils.hasText(nodeId) && StringUtils.hasText(event.stepKind())) {
                nodeId = DagStepRegistry.resolveNodeId(event.stepKind(), event.laneId());
            }
            Integer roundIndex = event.roundIndex();
            if (roundIndex == null && StringUtils.hasText(nodeId)) {
                roundIndex = roundCounters.merge(nodeId, 1, Integer::sum);
            } else if (roundIndex != null && StringUtils.hasText(nodeId)) {
                roundCounters.put(nodeId, Math.max(roundCounters.getOrDefault(nodeId, 0), roundIndex));
            }
            List<String> toolCalls = sanitizeToolCalls(event.toolCalls());
            String callKind = normalizeCallKind(event.callKind(), event.stepKind(), toolCalls, event.mcpCalls(), event.llmInvocationId());
            String callName = normalizeCallName(event.callName(), event.stepKind(), toolCalls, event.mcpCalls(), event.llmInvocationId());
            String roundRole = StringUtils.hasText(event.roundRole())
                    ? event.roundRole()
                    : inferRoundRole(event.stepKind(), callKind);
            String parentRoundId = StringUtils.hasText(nodeId) && roundIndex != null
                    ? nodeId + "#" + roundIndex
                    : event.parentRoundId();
            if (!sameSemantics(event, roundIndex, roundRole, callKind, callName, parentRoundId, toolCalls)) {
                enriched.add(withSemantics(event, roundIndex, roundRole, callKind, callName, parentRoundId, toolCalls));
            } else {
                enriched.add(event);
            }
        }
        return enriched;
    }

    private boolean sameSemantics(TraceEventResponse event, Integer roundIndex, String roundRole,
                                  String callKind, String callName, String parentRoundId, List<String> toolCalls) {
        return java.util.Objects.equals(event.roundIndex(), roundIndex)
                && java.util.Objects.equals(event.roundRole(), roundRole)
                && java.util.Objects.equals(event.callKind(), callKind)
                && java.util.Objects.equals(event.callName(), callName)
                && java.util.Objects.equals(event.parentRoundId(), parentRoundId)
                && java.util.Objects.equals(event.toolCalls(), toolCalls);
    }

    private String normalizeCallKind(String existing, String stepKind, List<String> toolCalls,
                                     List<String> mcpCalls, String llmInvocationId) {
        String inferred = inferCallKind(stepKind, toolCalls, mcpCalls, llmInvocationId);
        if ("skill".equals(existing) || !StringUtils.hasText(existing)) {
            return inferred;
        }
        if ("tool".equals(existing) && toolCalls.isEmpty() && !StringUtils.hasText(llmInvocationId)
                && (mcpCalls == null || mcpCalls.isEmpty())) {
            return inferred;
        }
        return existing;
    }

    private String normalizeCallName(String existing, String stepKind, List<String> toolCalls,
                                     List<String> mcpCalls, String llmInvocationId) {
        String inferred = inferCallName(stepKind, toolCalls, mcpCalls, llmInvocationId);
        if (!StringUtils.hasText(existing) || (existing != null && existing.endsWith("Skill"))) {
            return inferred;
        }
        if (existing != null && (existing.startsWith("{") || existing.contains("\"name\""))) {
            return inferred;
        }
        if ("tool".equals(existing) && toolCalls.isEmpty()) {
            return inferred;
        }
        return existing;
    }

    private TraceEventResponse withSemantics(TraceEventResponse event, Integer roundIndex, String roundRole,
                                             String callKind, String callName, String parentRoundId,
                                             List<String> toolCalls) {
        return new TraceEventResponse(
                event.traceId(), event.spanId(), event.parentSpanId(), event.agentRole(), event.eventType(),
                event.title(), event.detail(), event.status(), event.durationMs(), event.tokenCost(), event.timestamp(),
                event.dagGroupId(), event.laneId(), event.stepKind(), event.viewType(),
                event.businessLabel(), event.evidenceSummary(), event.interviewHints(),
                event.developerLabel(), event.skillName(), event.promptPreview(), event.inputSummary(), event.outputSummary(),
                toolCalls == null || toolCalls.isEmpty() ? null : toolCalls, event.mcpCalls(), event.executionSummary(), event.llmInvocationId(),
                event.nodeId(), event.dependsOn(), event.edgeLabel(), event.phase(), event.expected(), event.sortOrder(),
                event.fullPrompt(), event.fullInput(), event.fullOutput(),
                event.sequence(), roundIndex, roundRole, callKind, callName,
                event.parentAgentSpanId(), parentRoundId, event.ioJson());
    }

    private String buildIoJson(String fullInput, String fullOutput) {
        if (!StringUtils.hasText(fullInput) && !StringUtils.hasText(fullOutput)) {
            return null;
        }
        Map<String, Object> io = new LinkedHashMap<>();
        if (StringUtils.hasText(fullInput)) {
            io.put("input", fullInput);
        }
        if (StringUtils.hasText(fullOutput)) {
            io.put("output", fullOutput);
        }
        try {
            return objectMapper.writeValueAsString(io);
        } catch (Exception e) {
            return null;
        }
    }
    private String resolveTaskStatus(String traceId) {
        MutableTask task = tasks.get(traceId);
        if (task != null && StringUtils.hasText(task.status)) {
            return task.status;
        }
        try {
            ResumeTask row = resumeTaskMapper.selectOne(new QueryWrapper<ResumeTask>().eq("trace_id", traceId).last("limit 1"));
            if (row != null && StringUtils.hasText(row.getStatus())) {
                return row.getStatus();
            }
        } catch (DataAccessException e) {
            log.warn("[eval] resolve task status failed (trace={}): {}", traceId, e.getMessage());
        }
        return "RUNNING";
    }

    private List<TraceEventResponse> filterLegacyTraceDuplicates(List<TraceEventResponse> events) {
        if (events == null || events.isEmpty()) {
            return List.of();
        }
        boolean hasDag = events.stream().anyMatch(e ->
                StringUtils.hasText(e.stepKind()) || StringUtils.hasText(e.dagGroupId()));
        if (!hasDag) {
            return events;
        }
        return events.stream()
                .filter(e -> {
                    if (StringUtils.hasText(e.stepKind()) || StringUtils.hasText(e.dagGroupId())) {
                        return true;
                    }
                    if ("DAG_START".equals(e.eventType()) || "REPORT_READY".equals(e.eventType())
                            || "TASK_FAILED".equals(e.eventType()) || "TASK_CREATED".equals(e.eventType())) {
                        return StringUtils.hasText(e.stepKind());
                    }
                    return false;
                })
                .toList();
    }

    /**
     * 查询大盘指标。
     *
     * @return 仪表盘指标响应
     */
    /**
     * 大盘指标以数据库为准（一条聚合 SQL），不再依赖进程内 Map——
     * 重启、多实例、缓存未加载的行全部计入，修复“计数永远不变”。
     */
    public DashboardMetricsResponse metrics() {
        int total = 0;
        int running = 0;
        int success = 0;
        int failed = 0;
        int queued = 0;
        int completed = 0;
        int recommended = 0;
        int manualReview = 0;
        double avgDuration = 0D;
        double avgScore = 0D;
        int totalToken = 0;
        Map<String, Long> modeDuration = new LinkedHashMap<>();
        try {
            QueryWrapper<ResumeTask> aggregate = new QueryWrapper<>();
            aggregate.select(
                    "COUNT(*) AS total",
                    "COALESCE(SUM(status = 'RUNNING'), 0) AS running",
                    "COALESCE(SUM(status = 'SUCCESS'), 0) AS success",
                    "COALESCE(SUM(status = 'FAILED'), 0) AS failed",
                    "COALESCE(SUM(queue_status = 'QUEUED'), 0) AS queued",
                    "COALESCE(SUM(status = 'SUCCESS' AND recommendation IN ('RECOMMEND', 'STRONG_RECOMMEND')), 0) AS recommended",
                    "COALESCE(SUM(status = 'SUCCESS' AND (recommendation IS NULL OR recommendation NOT IN ('RECOMMEND', 'STRONG_RECOMMEND'))), 0) AS manual_review",
                    "COALESCE(AVG(duration_ms), 0) AS avg_duration",
                    "COALESCE(AVG(CASE WHEN status = 'SUCCESS' AND overall_score > 0 THEN overall_score END), 0) AS avg_score",
                    "COALESCE(SUM(token_cost), 0) AS total_token");
            Map<String, Object> row = resumeTaskMapper.selectMaps(aggregate).stream()
                    .filter(java.util.Objects::nonNull).findFirst().orElse(Map.of());
            total = metricInt(row.get("total"));
            running = metricInt(row.get("running"));
            success = metricInt(row.get("success"));
            failed = metricInt(row.get("failed"));
            queued = metricInt(row.get("queued"));
            completed = success;
            recommended = metricInt(row.get("recommended"));
            manualReview = metricInt(row.get("manual_review"));
            avgDuration = metricDouble(row.get("avg_duration"));
            avgScore = metricDouble(row.get("avg_score"));
            totalToken = metricInt(row.get("total_token"));

            QueryWrapper<ResumeTask> byMode = new QueryWrapper<>();
            byMode.select("execution_mode AS mode", "COALESCE(AVG(duration_ms), 0) AS avg_duration")
                    .isNotNull("execution_mode")
                    .groupBy("execution_mode");
            for (Map<String, Object> modeRow : resumeTaskMapper.selectMaps(byMode)) {
                if (modeRow == null) {
                    continue;
                }
                modeDuration.put(String.valueOf(modeRow.get("mode")),
                        (long) metricDouble(modeRow.get("avg_duration")));
            }
        } catch (Exception e) {
            log.warn("[eval] metrics db aggregate failed, falling back to cache: {}", e.getMessage());
            List<MutableTask> snapshot = List.copyOf(tasks.values());
            total = snapshot.size();
            running = (int) snapshot.stream().filter(task -> "RUNNING".equals(task.status)).count();
            success = (int) snapshot.stream().filter(task -> "SUCCESS".equals(task.status)).count();
            failed = (int) snapshot.stream().filter(task -> "FAILED".equals(task.status)).count();
            queued = (int) snapshot.stream().filter(task -> "QUEUED".equals(task.queueStatus)).count();
            completed = success;
            recommended = (int) snapshot.stream()
                    .filter(task -> "SUCCESS".equals(task.status)
                            && ("RECOMMEND".equals(task.recommendation) || "STRONG_RECOMMEND".equals(task.recommendation)))
                    .count();
            manualReview = (int) snapshot.stream()
                    .filter(task -> "SUCCESS".equals(task.status)
                            && !("RECOMMEND".equals(task.recommendation) || "STRONG_RECOMMEND".equals(task.recommendation)))
                    .count();
            avgDuration = snapshot.stream().mapToLong(task -> task.durationMs).average().orElse(0D);
            avgScore = snapshot.stream()
                    .filter(task -> "SUCCESS".equals(task.status) && task.overallScore != null && task.overallScore > 0)
                    .mapToInt(task -> task.overallScore)
                    .average().orElse(0D);
            totalToken = snapshot.stream().mapToInt(task -> task.tokenCost).sum();
        }
        modeDuration.putIfAbsent("SERIAL", 0L);
        modeDuration.putIfAbsent("DAG_CONCURRENT", 0L);
        Map<String, Long> durationSum = new LinkedHashMap<>();
        Map<String, Long> durationCount = new LinkedHashMap<>();
        traces.values().stream().flatMap(List::stream)
                .filter(event -> StringUtils.hasText(event.agentRole()) && event.durationMs() != null)
                .forEach(event -> {
                    durationSum.merge(event.agentRole(), event.durationMs(), Long::sum);
                    durationCount.merge(event.agentRole(), 1L, Long::sum);
                });
        Map<String, Long> agentDuration = new LinkedHashMap<>();
        durationSum.forEach((agent, duration) ->
                agentDuration.put(agent, duration / Math.max(1L, durationCount.getOrDefault(agent, 1L))));
        int uniqueCandidates = candidateService.countUniqueCandidates();
        return new DashboardMetricsResponse(
                total, running, success, failed,
                queued, completed, recommended, manualReview,
                avgDuration, avgScore, totalToken, modeDuration, agentDuration,
                uniqueCandidates);
    }

    private static int metricInt(Object value) {
        return value instanceof Number n ? n.intValue() : 0;
    }

    private static double metricDouble(Object value) {
        return value instanceof Number n ? n.doubleValue() : 0D;
    }



    private void executeTask(MutableTask task) {
        executeTaskViaAgentRuntime(task);
    }

    /**
     * Every uploaded-resume evaluation runs through the unified agent runtime:
     * ensure a conversation session exists for the task, enqueue one
     * full-evaluation run linked back via sourceTaskTraceId, and let the
     * shared scheduler drive it (same queue, permits, watchdog, recovery).
     */
    private void executeTaskViaAgentRuntime(MutableTask task) {
        try {
            String conversationId = ensureTaskConversation(task);
            String runId = StringUtils.hasText(task.workflowRunId)
                    ? task.workflowRunId : "run-" + UUID.randomUUID();
            task.workflowRunId = runId;
            String userMessage = StringUtils.hasText(task.evaluationBrief)
                    ? "请对这份简历进行完整评估。补充要求：" + task.evaluationBrief
                    : "请对这份简历进行完整评估，输出技术、项目、风险、证据与录用建议。";
            runQueueService.enqueueTaskRun(
                    runId, conversationId, task.uploadedBy, task.traceId,
                    Math.max(1, task.revisionNo),
                    runTypeForTask(task), userMessage, task.traceId, 0, task.planMode);
            runSchedulerService.kick();
            task.summary = "已进入统一 Agent 运行队列，正在异步评估。";
            task.updateTime = LocalDateTime.now();
            updateResumeTask(task);
        } catch (Exception e) {
            task.status = "FAILED";
            task.queueStatus = QueueStatus.FAILED.name();
            task.summary = "启动 Agent 运行失败：" + e.getMessage();
            task.finishedAt = LocalDateTime.now();
            task.updateTime = LocalDateTime.now();
            updateResumeTask(task);
            runtimeStateService.evictRunningTask(task.traceId);
        }
    }

    private String runTypeForTask(MutableTask task) {
        String category = task.jobCategory != null ? task.jobCategory.toLowerCase() : "";
        if (category.contains("java") || category.contains("backend") || category.contains("后端")) {
            return "backend_eval";
        }
        if (category.contains("agent") || category.contains("ai")) {
            return "agent_eval";
        }
        return "full_evaluation";
    }

    /** The task's conversation: reuse if present, otherwise create a session
     * seeded with the task's resume/JD snapshot. */
    private String ensureTaskConversation(MutableTask task) {
        String conversationId = StringUtils.hasText(task.conversationId)
                ? task.conversationId : "conv-task-" + task.traceId;
        task.conversationId = conversationId;
        ConversationSession session = conversationSessionMapper.selectById(conversationId);
        if (session == null) {
            session = new ConversationSession();
            session.setId(conversationId);
            session.setUserId(StringUtils.hasText(task.uploadedBy) ? task.uploadedBy : "demo-hr");
            session.setTitle("简历任务 " + task.traceId);
            session.setResumeText(task.resumeText);
            session.setJobDescription(task.jobDescription);
            session.setJobCategory(task.jobCategory);
            session.setActiveTraceId(task.traceId);
            session.setActiveRevision(Math.max(1, task.revisionNo));
            session.setCreateTime(LocalDateTime.now());
            session.setUpdateTime(LocalDateTime.now());
            session.setDeleted(0);
            conversationSessionMapper.insert(session);
        } else {
            session.setResumeText(task.resumeText);
            session.setJobDescription(task.jobDescription);
            session.setJobCategory(task.jobCategory);
            session.setUpdateTime(LocalDateTime.now());
            conversationSessionMapper.updateById(session);
        }
        return conversationId;
    }

    /** Best-effort cancellation of the linked agent run (Java stays authoritative). */
    private void cancelLinkedRun(String runId, String reasonCode, String reasonText) {
        try {
            AgentRun run = runLifecycleService.getRun(runId);
            if (run != null && !RunStatus.isTerminal(run.getStatus())) {
                if (RunStatus.QUEUED.name().equals(run.getStatus())) {
                    runQueueService.cancelQueued(runId, reasonCode);
                } else {
                    runLifecycleService.cancelActiveRun(run, reasonCode, reasonText);
                }
            }
        } catch (Exception e) {
            log.info("[eval] linked run cancellation deferred run={}: {}", runId, e.getMessage());
        }
    }



    private void appendTrace(String traceId, String parentSpanId, String agentRole, String eventType, String title, String detail, String status, Long durationMs, Integer tokenCost) {
        appendDagTrace(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                null, null, null, "BOTH", null, null, null, null, null, null, null, null, null, null);
    }

    private void appendDagTrace(String traceId, String parentSpanId, String agentRole, String eventType,
                                String title, String detail, String status, Long durationMs, Integer tokenCost,
                                String dagGroupId, String laneId, String stepKind, String viewType,
                                String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                String developerLabel, String skillName, String promptPreview,
                                String inputSummary, String outputSummary,
                                java.util.List<String> toolCalls, java.util.List<String> mcpCalls) {
        appendDagTrace(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                dagGroupId, laneId, stepKind, viewType, businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, promptPreview, inputSummary, outputSummary, toolCalls, mcpCalls, null,
                null, null, null, null);
    }

    private void appendDagTrace(String traceId, String parentSpanId, String agentRole, String eventType,
                                String title, String detail, String status, Long durationMs, Integer tokenCost,
                                String dagGroupId, String laneId, String stepKind, String viewType,
                                String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                String developerLabel, String skillName, String promptPreview,
                                String inputSummary, String outputSummary,
                                java.util.List<String> toolCalls, java.util.List<String> mcpCalls,
                                String llmInvocationId) {
        appendDagTrace(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                dagGroupId, laneId, stepKind, viewType, businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, promptPreview, inputSummary, outputSummary, toolCalls, mcpCalls, null,
                llmInvocationId, null, null, null);
    }

    private void appendDagTrace(String traceId, String parentSpanId, String agentRole, String eventType,
                                String title, String detail, String status, Long durationMs, Integer tokenCost,
                                String dagGroupId, String laneId, String stepKind, String viewType,
                                String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                String developerLabel, String skillName, String promptPreview,
                                String inputSummary, String outputSummary,
                                java.util.List<String> toolCalls, java.util.List<String> mcpCalls,
                                String executionSummary, String llmInvocationId,
                                String fullPrompt, String fullInput, String fullOutput) {
        appendDagTraceFull(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                dagGroupId, laneId, stepKind, viewType, businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, promptPreview, inputSummary, outputSummary, toolCalls, mcpCalls,
                executionSummary, llmInvocationId, fullPrompt, fullInput, fullOutput);
    }

    private void appendDagTraceFull(String traceId, String parentSpanId, String agentRole, String eventType,
                                    String title, String detail, String status, Long durationMs, Integer tokenCost,
                                    String dagGroupId, String laneId, String stepKind, String viewType,
                                    String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                    String developerLabel, String skillName, String promptPreview,
                                    String inputSummary, String outputSummary,
                                    java.util.List<String> toolCalls, java.util.List<String> mcpCalls,
                                    String executionSummary, String llmInvocationId) {
        appendDagTraceFull(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                dagGroupId, laneId, stepKind, viewType, businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, promptPreview, inputSummary, outputSummary, toolCalls, mcpCalls,
                executionSummary, llmInvocationId, null, null, null);
    }

    private void appendDagTraceFull(String traceId, String parentSpanId, String agentRole, String eventType,
                                    String title, String detail, String status, Long durationMs, Integer tokenCost,
                                    String dagGroupId, String laneId, String stepKind, String viewType,
                                    String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                    String developerLabel, String skillName, String promptPreview,
                                    String inputSummary, String outputSummary,
                                    java.util.List<String> toolCalls, java.util.List<String> mcpCalls,
                                    String executionSummary, String llmInvocationId,
                                    String fullPrompt, String fullInput, String fullOutput) {
        String spanId = "span-" + UUID.randomUUID();
        LocalDateTime now = LocalDateTime.now();
        StepDefinition stepDefinition = DagStepRegistry.findByStepKind(stepKind, laneId).orElse(null);
        String nodeId = stepDefinition != null ? stepDefinition.nodeId() : DagStepRegistry.resolveNodeId(stepKind, laneId);
        List<String> dependsOn = stepDefinition != null ? stepDefinition.dependsOn() : DagStepRegistry.dependsOnFor(stepKind, laneId);
        String edgeLabel = stepDefinition != null ? stepDefinition.edgeLabel() : null;
        String phase = stepDefinition != null ? stepDefinition.phase() : null;
        Integer sortOrder = stepDefinition != null ? stepDefinition.sortOrder() : null;
        String previewPrompt = StringUtils.hasText(promptPreview) ? promptPreview : trim(fullPrompt, 200);
        String previewInput = StringUtils.hasText(inputSummary) ? inputSummary : trim(fullInput, 120);
        String previewOutput = StringUtils.hasText(outputSummary) ? outputSummary : trim(fullOutput, 120);
        int sequence = nextSequence(traceId);
        List<String> sanitizedToolCalls = sanitizeToolCalls(toolCalls);
        String callKind = inferCallKind(stepKind, sanitizedToolCalls, mcpCalls, llmInvocationId);
        String callName = inferCallName(stepKind, sanitizedToolCalls, mcpCalls, llmInvocationId);
        int roundIndex = nextRoundIndex(traceId, nodeId);
        String roundRole = inferRoundRole(stepKind, callKind);
        String parentRoundId = nodeId + "#" + roundIndex;
        String ioJson = buildIoJson(fullInput, fullOutput);
        String parentAgentSpanId = parentSpanId;
        TraceEventResponse event = new TraceEventResponse(
                traceId, spanId, parentSpanId, agentRole, eventType, title, detail, status,
                durationMs, tokenCost, now,
                dagGroupId, laneId, stepKind, viewType,
                businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, previewPrompt, previewInput, previewOutput,
                sanitizedToolCalls.isEmpty() ? null : sanitizedToolCalls, mcpCalls, executionSummary, llmInvocationId,
                nodeId, dependsOn, edgeLabel, phase, false, sortOrder,
                fullPrompt, fullInput, fullOutput,
                sequence, roundIndex, roundRole, callKind, callName, parentAgentSpanId, parentRoundId, ioJson
        );
        traces.computeIfAbsent(traceId, ignored -> new ArrayList<>()).add(event);
        sseTraceHub.publish(event);
        try {
            AgentExecutionTrace entity = new AgentExecutionTrace();
            entity.setTraceId(traceId);
            entity.setSpanId(spanId);
            entity.setParentSpanId(parentSpanId);
            entity.setAgentRole(agentRole);
            entity.setSkillName(skillName);
            entity.setToolCall(eventType);
            entity.setInputSummary(previewInput != null ? previewInput : title);
            entity.setOutputSummary(previewOutput != null ? previewOutput : detail);
            entity.setStatus(status);
            entity.setDurationMs(durationMs);
            entity.setCostTokens(tokenCost == null ? null : tokenCost.longValue());
            entity.setRetryCount(0);
            entity.setCreateTime(now);
            entity.setUpdateTime(now);
            entity.setPayload(buildTracePayloadJson(event));
            agentExecutionTraceMapper.insert(entity);
        } catch (DataAccessException e) {
            log.warn("[eval] persist agent_execution_trace failed (trace={}): {}", traceId, e.getMessage());
        }
    }

    private String buildTracePayloadJson(TraceEventResponse event) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("agentRole", event.agentRole());
        payload.put("eventType", event.eventType());
        payload.put("title", event.title());
        payload.put("detail", event.detail());
        payload.put("status", event.status());
        payload.put("dagGroupId", event.dagGroupId());
        payload.put("laneId", event.laneId());
        payload.put("stepKind", event.stepKind());
        payload.put("viewType", event.viewType());
        payload.put("businessLabel", event.businessLabel());
        payload.put("evidenceSummary", event.evidenceSummary());
        payload.put("interviewHints", event.interviewHints());
        payload.put("developerLabel", event.developerLabel());
        payload.put("skillName", event.skillName());
        payload.put("promptPreview", event.promptPreview());
        payload.put("inputSummary", event.inputSummary());
        payload.put("outputSummary", event.outputSummary());
        payload.put("toolCalls", event.toolCalls());
        payload.put("mcpCalls", event.mcpCalls());
        payload.put("executionSummary", event.executionSummary());
        payload.put("llmInvocationId", event.llmInvocationId());
        payload.put("nodeId", event.nodeId());
        payload.put("dependsOn", event.dependsOn());
        payload.put("edgeLabel", event.edgeLabel());
        payload.put("phase", event.phase());
        payload.put("expected", event.expected());
        payload.put("sortOrder", event.sortOrder());
        payload.put("fullPrompt", event.fullPrompt());
        payload.put("fullInput", event.fullInput());
        payload.put("fullOutput", event.fullOutput());
        payload.put("sequence", event.sequence());
        payload.put("roundIndex", event.roundIndex());
        payload.put("roundRole", event.roundRole());
        payload.put("callKind", event.callKind());
        payload.put("callName", event.callName());
        payload.put("parentAgentSpanId", event.parentAgentSpanId());
        payload.put("parentRoundId", event.parentRoundId());
        payload.put("ioJson", event.ioJson());
        try {
            return objectMapper.writeValueAsString(payload);
        } catch (Exception e) {
            log.warn("[eval] serialize trace payload failed: {}", e.getMessage());
            return null;
        }
    }

    private String buildPrompt(MutableTask task) {
        return buildPrompt(task, "", "");
    }

    private String buildPrompt(MutableTask task, String ragContext, String enrichmentContext) {
        String ragSection = StringUtils.hasText(ragContext)
                ? "\n向量检索证据：\n" + ragContext + "\n"
                : "";
        String enrichSection = StringUtils.hasText(enrichmentContext)
                ? "\n" + enrichmentContext + "\n"
                : "";
        return """
                请基于以下信息生成企业招聘场景的简历评估报告，要求包含：综合评分、推荐结论、优势、风险、面试追问。
                如果候选人有外部作品（GitHub 项目、技术博客等），请在评估中重点参考其代码质量和技术深度。

                候选人文件名：%s
                岗位类别：%s
                执行模式：%s
                岗位描述：%s
                简历文本：%s
                %s%s""".formatted(
                task.fileName,
                task.jobCategory,
                task.executionMode,
                StringUtils.hasText(task.jobDescription) ? task.jobDescription : "未填写岗位描述，请按通用技术岗位标准评估。",
                StringUtils.hasText(task.resumeText) ? task.resumeText : "未填写简历正文，请基于文件名和岗位类别给出低风险评估。",
                ragSection,
                enrichSection
        );
    }

    private SystemOrchestrationRule loadRule(String jobCategory) {
        try {
            SystemOrchestrationRule rule = ruleMapper.selectOne(
                    new QueryWrapper<SystemOrchestrationRule>()
                            .eq("job_category", jobCategory)
                            .eq("enabled", 1)
                            .orderByDesc("version")
                            .last("limit 1"));
            return rule;
        } catch (Exception e) {
            log.warn("加载编排规则失败(jobCategory={}): {}", jobCategory, e.getMessage());
            return null;
        }
    }

    private String resolveSkillPrompt(String agent, String skillName, String fallback) {
        try {
            DynamicSkillPrompt skill = skillPromptMapper.selectOne(
                    new QueryWrapper<DynamicSkillPrompt>()
                            .eq("skill_name", skillName)
                            .eq("enabled", 1)
                            .orderByDesc("version")
                            .last("limit 1"));
            boolean found = skill != null && StringUtils.hasText(skill.getPromptTemplate());
            if (found) {
                return skill.getPromptTemplate();
            }
        } catch (Exception e) {
            log.warn("加载 Skill Prompt 失败(skill={}): {}", skillName, e.getMessage());
        }
        return fallback;
    }

    private String buildWorkflowSummaryFallback(List<String> strengths, List<String> risks, List<String> questions) {
        StringBuilder sb = new StringBuilder();
        if (strengths != null && !strengths.isEmpty()) {
            sb.append("关键优势：\n");
            strengths.stream().limit(3).forEach(item -> sb.append("- ").append(item).append('\n'));
        }
        if (risks != null && !risks.isEmpty()) {
            if (!sb.isEmpty()) {
                sb.append('\n');
            }
            sb.append("关键风险：\n");
            risks.stream().limit(3).forEach(item -> sb.append("- ").append(item).append('\n'));
        }
        if (questions != null && !questions.isEmpty()) {
            if (!sb.isEmpty()) {
                sb.append('\n');
            }
            sb.append("建议追问：\n");
            questions.stream().limit(3).forEach(item -> sb.append("- ").append(item).append('\n'));
        }
        return sb.isEmpty() ? "评估完成，但最终报告为空，请检查 ReportAgent 输出。" : sb.toString().trim();
    }

    private String buildListSummary(String fullReport, Integer score, String recommendation) {
        String firstSignal = "";
        if (StringUtils.hasText(fullReport)) {
            for (String line : fullReport.split("\\R")) {
                String stripped = line.replaceAll("[#*_`>\\-]+", "").trim();
                if (stripped.length() >= 12
                        && !stripped.startsWith("综合评分")
                        && !stripped.startsWith("推荐决策")
                        && !stripped.startsWith("证据来源")) {
                    firstSignal = stripped;
                    break;
                }
            }
        }
        String prefix = "评估完成";
        if (score != null) {
            prefix += "，综合评分 " + score + "/100";
        }
        if (StringUtils.hasText(recommendation)) {
            prefix += "，结论 " + recommendation;
        }
        return trim(StringUtils.hasText(firstSignal) ? prefix + "。" + firstSignal : prefix, 500);
    }

    private String normalizeRecommendation(String recommendation, Integer score) {
        String rec = StringUtils.hasText(recommendation) ? recommendation : "NEED_MANUAL_REVIEW";
        int safeScore = score != null ? score : 0;
        if (safeScore < 60) {
            return "NOT_RECOMMEND";
        }
        if (safeScore < 75 && ("RECOMMEND".equals(rec) || "STRONG_RECOMMEND".equals(rec))) {
            return "NEED_MANUAL_REVIEW";
        }
        if (safeScore < 85 && "STRONG_RECOMMEND".equals(rec)) {
            return "RECOMMEND";
        }
        return rec;
    }


    private TaskResponse toResponse(MutableTask task) {
        String resumeFileUrl = StringUtils.hasText(task.resumeFilePath) || StringUtils.hasText(task.resumeFileType)
                ? "/api/tasks/" + task.traceId + "/file"
                : null;
        AgentRun linkedRun = resolveLinkedRun(task);
        String evaluationState = resolveEvaluationState(task.status);
        TaskSystemError systemError = null;
        if ("SYSTEM_FAILED".equals(evaluationState) && linkedRun != null) {
            String code = linkedRun.getErrorCode();
            systemError = new TaskSystemError(
                    code,
                    stageForSystemError(code),
                    isRetryableSystemError(code),
                    linkedRun.getErrorMessage(),
                    linkedRun.getRunId());
        }
        // Keep recommendation null for control-plane / terminal system failures.
        // For business COMPLETED (SUCCESS/PARTIAL_SUCCESS), missing structured
        // recommendation means the specialist pipeline ran but ReportAgent did
        // not emit a contract — surface NEED_MANUAL_REVIEW (not a fake control-plane wrap).
        String recommendation = null;
        if (task.structuredReport != null
                && task.structuredReport.get("recommendation") instanceof String structuredRec
                && StringUtils.hasText(structuredRec)) {
            recommendation = normalizeRecommendation(structuredRec, task.overallScore);
        } else if (StringUtils.hasText(task.recommendation)
                && !"SYSTEM_FAILED".equals(evaluationState)
                && !"FAILED".equals(task.status)
                && !"TIMED_OUT".equals(task.status)) {
            recommendation = task.recommendation;
        } else if ("COMPLETED".equals(evaluationState)) {
            recommendation = "NEED_MANUAL_REVIEW";
        }
        return new TaskResponse(task.id, task.traceId, task.fileName, task.jobCategory, task.executionMode, task.status,
                task.overallScore, recommendation, task.summary, task.durationMs, task.tokenCost,
                task.createTime, task.updateTime, task.strengths, task.risks, task.interviewQuestions, task.resumeText,
                resumeFileUrl, task.resumeFileType, task.matchedJdTitle, task.jdMatchScore, task.topJdMatches,
                task.aiRecommendation, task.decisionRationale, task.riskSummary, buildQueueFields(task),
                task.conversationId, task.revisionNo, task.workflowRunId, task.baseWorkflowRunId, task.supersedesTraceId,
                task.supersededByTraceId, task.evaluationBrief,
                task.invalidatedNodes != null ? task.invalidatedNodes : List.of(),
                task.finalReport,
                task.structuredReport != null ? task.structuredReport : Map.of(),
                task.candidateId,
                task.applicationId,
                evaluationState,
                systemError);
    }

    private AgentRun resolveLinkedRun(MutableTask task) {
        if (task == null) {
            return null;
        }
        try {
            if (StringUtils.hasText(task.workflowRunId)) {
                AgentRun byId = agentRunMapper.selectById(task.workflowRunId);
                if (byId != null) {
                    return byId;
                }
            }
            if (StringUtils.hasText(task.traceId)) {
                return agentRunMapper.selectOne(new QueryWrapper<AgentRun>()
                        .eq("source_task_trace_id", task.traceId)
                        .orderByDesc("created_at")
                        .last("limit 1"));
            }
        } catch (Exception e) {
            log.debug("[eval] linked run lookup failed trace={}: {}", task.traceId, e.getMessage());
        }
        return null;
    }

    private static String resolveEvaluationState(String status) {
        if (!StringUtils.hasText(status)) {
            return "NOT_STARTED";
        }
        return switch (status) {
            case "SUCCESS", "PARTIAL_SUCCESS" -> "COMPLETED";
            case "FAILED", "TIMED_OUT" -> "SYSTEM_FAILED";
            case "QUEUED", "RUNNING", "STARTING", "PAUSING", "PAUSED", "RESUMING",
                 "WAITING_LLM", "WAITING_TOOL", "CANCELLING", "RETRYING" -> "RUNNING";
            default -> "NOT_STARTED";
        };
    }

    private static String stageForSystemError(String errorCode) {
        if (errorCode == null) {
            return "unknown";
        }
        return switch (errorCode) {
            case "POLICY_SELECTION_PERSIST_FAILED" -> "policy_selection";
            case "RUNTIME_START_FAILED", "START_STUCK" -> "start";
            case "ORPHANED_ON_RESTART" -> "restart_recovery";
            case "RUN_TIMEOUT" -> "watchdog";
            case "CANCEL_FORCED" -> "cancel";
            case "PAUSE_EXPIRED" -> "pause";
            default -> "runtime";
        };
    }

    private static boolean isRetryableSystemError(String errorCode) {
        if (errorCode == null) {
            return false;
        }
        return switch (errorCode) {
            case "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED", "START_STUCK",
                 "RUN_TIMEOUT", "POLICY_SELECTION_PERSIST_FAILED" -> true;
            default -> false;
        };
    }

    private String normalizeExecutionMode(String executionMode) {
        return "DAG_CONCURRENT".equalsIgnoreCase(executionMode) ? "DAG_CONCURRENT" : "SERIAL";
    }

    private String normalizeJobCategory(String jobCategory) {
        return StringUtils.hasText(jobCategory) ? jobCategory.trim().toUpperCase() : "TECH";
    }







    private String stringValue(Object value, String fallback) {
        return value == null ? fallback : String.valueOf(value);
    }

    /** Prefer non-blank payload value; otherwise fall back (may be null). */
    private String nullableString(Object value, String fallback) {
        if (value == null) {
            return fallback;
        }
        String text = String.valueOf(value).trim();
        return StringUtils.hasText(text) ? text : fallback;
    }

    private Boolean boolValue(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Boolean b) {
            return b;
        }
        return Boolean.parseBoolean(String.valueOf(value));
    }

    private int intValue(Object value, int fallback) {
        if (value instanceof Number number) {
            return number.intValue();
        }
        return fallback;
    }

    /** Nullable score/fields: missing value must stay null (never coerce to 0). */
    private Integer nullableInteger(Object value, Integer fallback) {
        if (value instanceof Number number) {
            return number.intValue();
        }
        if (value instanceof String text && StringUtils.hasText(text)) {
            try {
                return Integer.parseInt(text.trim());
            } catch (NumberFormatException ignored) {
                return fallback;
            }
        }
        return fallback;
    }

    private long longValue(Object value, long fallback) {
        if (value instanceof Number number) {
            return number.longValue();
        }
        return fallback;
    }

    private Double doubleValue(Object value) {
        if (value instanceof Number number) {
            return number.doubleValue();
        }
        return null;
    }

    private List<String> stringList(Object value) {
        if (value instanceof List<?> list) {
            List<String> out = new ArrayList<>();
            for (Object item : list) {
                if (item instanceof String s) {
                    if (StringUtils.hasText(s)) {
                        out.add(s);
                    }
                } else if (item instanceof Map<?, ?> map) {
                    Object claim = map.get("claim");
                    Object question = map.get("question");
                    Object preferred = claim != null ? claim : question;
                    if (preferred != null && StringUtils.hasText(String.valueOf(preferred))) {
                        out.add(String.valueOf(preferred));
                    }
                } else if (item != null) {
                    out.add(String.valueOf(item));
                }
            }
            return out;
        }
        return new ArrayList<>();
    }

    private List<String> parseJsonStringList(String value) {
        if (!StringUtils.hasText(value)) {
            return List.of();
        }
        try {
            return objectMapper.readValue(value, new TypeReference<List<String>>() {});
        } catch (Exception e) {
            return List.of();
        }
    }

    private String toJson(Object value) {
        if (value == null) {
            return null;
        }
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            log.debug("[eval] could not serialize task metadata: {}", e.getMessage());
            return null;
        }
    }

    private String trim(String value, int maxLength) {
        if (value == null || value.length() <= maxLength) {
            return value;
        }
        return value.substring(0, maxLength) + "...";
    }

    private String toPrettyJson(Map<String, Object> payload) {
        try {
            return objectMapper.writerWithDefaultPrettyPrinter().writeValueAsString(payload);
        } catch (Exception e) {
            return String.valueOf(payload);
        }
    }

    private String detectFileType(String fileName) {
        if (!StringUtils.hasText(fileName)) {
            return "unknown";
        }
        String lower = fileName.toLowerCase();
        if (lower.endsWith(".pdf")) {
            return "pdf";
        }
        if (lower.endsWith(".txt")) {
            return "txt";
        }
        if (lower.endsWith(".md")) {
            return "md";
        }
        if (lower.endsWith(".csv")) {
            return "csv";
        }
        return "other";
    }

    private static final class MutableTask {
        private final Long id;
        private final String traceId;
        private final String fileName;
        private String jobCategory;
        private final String executionMode;
        private String status;
        private Integer overallScore;
        private String recommendation;
        private String summary;
        private Long durationMs;
        private Integer tokenCost;
        private final LocalDateTime createTime;
        private LocalDateTime updateTime;
        private List<String> strengths;
        private List<String> risks;
        private List<String> interviewQuestions;
        private String jobDescription;
        private final String resumeText;
        private String resumeFilePath;
        private String resumeFileType;
        private String matchedJdTitle;
        private Double jdMatchScore;
        private List<JdMatchResult> topJdMatches;
        private String aiRecommendation;
        private String decisionRationale;
        private String riskSummary;
        private String finalReport;
        private Map<String, Object> structuredReport = Map.of();
        private RagOptions ragOptions;
        private String uploadedBy;
        private String tenantId;
        private int priority;
        private String queueStatus;
        private LocalDateTime queuedAt;
        private LocalDateTime startedAt;
        private LocalDateTime finishedAt;
        private int attemptCount;
        private LocalDateTime nextRetryAt;
        private String workerId;
        private String conversationId;
        private int revisionNo = 1;
        private String workflowRunId;
        private String baseWorkflowRunId;
        private String supersedesTraceId;
        private String supersededByTraceId;
        private String evaluationBrief;
        private boolean planMode;
        private List<String> invalidatedNodes = List.of();
        private Long candidateId;
        private Long applicationId;

        private MutableTask(Long id, String traceId, String fileName, String jobCategory, String executionMode, String status,
                            Integer overallScore, String recommendation, String summary, Long durationMs, Integer tokenCost,
                            LocalDateTime createTime, LocalDateTime updateTime, List<String> strengths, List<String> risks,
                            List<String> interviewQuestions, String jobDescription, String resumeText,
                            String resumeFilePath, String resumeFileType,
                            String matchedJdTitle, Double jdMatchScore, List<JdMatchResult> topJdMatches,
                            String aiRecommendation, String decisionRationale, String riskSummary) {
            this.id = id;
            this.traceId = traceId;
            this.fileName = fileName;
            this.jobCategory = jobCategory;
            this.executionMode = executionMode;
            this.status = status;
            this.overallScore = overallScore;
            this.recommendation = recommendation;
            this.summary = summary;
            this.durationMs = durationMs;
            this.tokenCost = tokenCost;
            this.createTime = createTime;
            this.updateTime = updateTime;
            this.strengths = strengths;
            this.risks = risks;
            this.interviewQuestions = interviewQuestions;
            this.jobDescription = jobDescription;
            this.resumeText = resumeText;
            this.resumeFilePath = resumeFilePath;
            this.resumeFileType = resumeFileType;
            this.matchedJdTitle = matchedJdTitle;
            this.jdMatchScore = jdMatchScore;
            this.topJdMatches = topJdMatches;
            this.aiRecommendation = aiRecommendation;
            this.decisionRationale = decisionRationale;
            this.riskSummary = riskSummary;
        }
    }

    private record RevisionContext(
            String conversationId,
            int revisionNo,
            String supersedesTraceId,
            String baseWorkflowRunId,
            String evaluationBrief,
            List<String> invalidatedNodes
    ) {
    }

    private List<String> formatToolCall(String name, String input, String output, long durationMs, String status) {
        return List.of(String.format("{\"name\":\"%s\",\"durationMs\":%d,\"status\":\"%s\",\"inputSummary\":\"%s\",\"outputSummary\":\"%s\"}",
                name, durationMs, status, escapeJson(trim(input, 120)), escapeJson(trim(output, 120))));
    }

    private List<String> formatMcpCall(String target, String input, String output, long durationMs, boolean success) {
        return List.of(String.format("{\"server\":\"%s\",\"durationMs\":%d,\"status\":\"%s\",\"inputSummary\":\"%s\",\"outputSummary\":\"%s\"}",
                target, durationMs, success ? "SUCCESS" : "FAILED", escapeJson(trim(input, 80)), escapeJson(trim(output, 120))));
    }

    private String escapeJson(String value) {
        if (value == null) return "";
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
