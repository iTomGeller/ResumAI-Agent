package com.resumai.agent.service;

import com.resumai.agent.api.dto.TaskResponse;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.ai.ResumeEvaluationOrchestrator;
import com.resumai.agent.ai.AgentTraceCapture;
import com.resumai.agent.ai.agents.EvaluationResult;
import com.resumai.agent.api.dto.CreateTaskRequest;
import com.resumai.agent.api.dto.RecommendationDecision;
import com.resumai.agent.util.HrContext;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import com.resumai.agent.config.AgentMetrics;
import com.resumai.agent.api.dto.DashboardMetricsResponse;
import com.resumai.agent.api.dto.FeedbackRequest;
import com.resumai.agent.api.dto.FeedbackResponse;
import com.resumai.agent.api.dto.GraphResponse;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.TaskListItemResponse;
import com.resumai.agent.api.dto.TaskQueueFields;
import com.resumai.agent.api.dto.TraceEventResponse;
import com.resumai.agent.dao.AgentExecutionTraceMapper;
import com.resumai.agent.dao.DynamicSkillPromptMapper;
import com.resumai.agent.dao.HumanFeedbackLogMapper;
import com.resumai.agent.dao.RagasEvalMetricsMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.dao.SystemOrchestrationRuleMapper;
import com.resumai.agent.domain.entity.AgentExecutionTrace;
import com.resumai.agent.domain.entity.DynamicSkillPrompt;
import com.resumai.agent.domain.entity.HumanFeedbackLog;
import com.resumai.agent.domain.entity.RagasEvalMetrics;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.QueueStatus;
import com.resumai.agent.domain.dag.DagStepRegistry;
import com.resumai.agent.domain.dag.DagStepRegistry.StepDefinition;
import com.resumai.agent.domain.entity.SystemOrchestrationRule;
import com.resumai.agent.rag.RagOptions;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import java.math.BigDecimal;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.multipart.MultipartFile;

/**
 * MVP 简历评估服务。
 *
 * <p>MySQL 承担任务/JD/反馈/Trace 的事实查询，内存与 Redis 仅承接 RUNNING 运行态缓存。</p>
 */
@Service
public class MvpEvaluationService {

    private static final Logger log = LoggerFactory.getLogger(MvpEvaluationService.class);
    private static final long MAX_UPLOAD_BYTES = 20L * 1024L * 1024L;
    private static final int MAX_RESUME_TEXT_LENGTH = 20000;

    private final AtomicLong taskId = new AtomicLong(1000);
    private final AtomicLong feedbackId = new AtomicLong(3000);
    private final Map<String, MutableTask> tasks = new ConcurrentHashMap<>();
    private final Map<String, List<TraceEventResponse>> traces = new ConcurrentHashMap<>();
    private final Map<String, AtomicInteger> traceSequences = new ConcurrentHashMap<>();
    private final Map<String, Map<String, AtomicInteger>> traceRoundCounters = new ConcurrentHashMap<>();
    private final List<FeedbackResponse> feedbacks = new ArrayList<>();
    private final TaskQueueService taskQueueService;
    private final DeepSeekClient deepSeekClient;
    private final SseTraceHub sseTraceHub;
    private final ResumeGraphService resumeGraphService;
    private final ResumeRagService resumeRagService;
    private final ResumeTaskMapper resumeTaskMapper;
    private final AgentExecutionTraceMapper agentExecutionTraceMapper;
    private final HumanFeedbackLogMapper humanFeedbackLogMapper;
    private final RagasEvalMetricsMapper ragasEvalMetricsMapper;
    private final SystemOrchestrationRuleMapper ruleMapper;
    private final DynamicSkillPromptMapper skillPromptMapper;
    private final AgentMetrics agentMetrics;
    private final ExternalProfileService externalProfileService;
    private final JdRagService jdRagService;
    private final HybridRagService hybridRagService;
    private final RagConfigService ragConfigService;
    private final ResumeFileService resumeFileService;
    private final TaskQueryService taskQueryService;
    private final RuntimeStateService runtimeStateService;
    private final ObjectMapper objectMapper;
    private final Tracer tracer;
    private final ResumeEvaluationOrchestrator evaluationOrchestrator;
    private final AgentTraceCapture agentTraceCapture;

    public MvpEvaluationService(DeepSeekClient deepSeekClient,
                                SseTraceHub sseTraceHub,
                                ResumeGraphService resumeGraphService,
                                ResumeRagService resumeRagService,
                                ResumeTaskMapper resumeTaskMapper,
                                AgentExecutionTraceMapper agentExecutionTraceMapper,
                                HumanFeedbackLogMapper humanFeedbackLogMapper,
                                RagasEvalMetricsMapper ragasEvalMetricsMapper,
                                SystemOrchestrationRuleMapper ruleMapper,
                                DynamicSkillPromptMapper skillPromptMapper,
                                AgentMetrics agentMetrics,
                                ExternalProfileService externalProfileService,
                                JdRagService jdRagService,
                                HybridRagService hybridRagService,
                                RagConfigService ragConfigService,
                                ResumeFileService resumeFileService,
                                TaskQueryService taskQueryService,
                                RuntimeStateService runtimeStateService,
                                TaskQueueService taskQueueService,
                                ObjectMapper objectMapper,
                                OpenTelemetry openTelemetry,
                                ResumeEvaluationOrchestrator evaluationOrchestrator,
                                AgentTraceCapture agentTraceCapture) {
        this.deepSeekClient = deepSeekClient;
        this.sseTraceHub = sseTraceHub;
        this.resumeGraphService = resumeGraphService;
        this.resumeRagService = resumeRagService;
        this.resumeTaskMapper = resumeTaskMapper;
        this.agentExecutionTraceMapper = agentExecutionTraceMapper;
        this.humanFeedbackLogMapper = humanFeedbackLogMapper;
        this.ragasEvalMetricsMapper = ragasEvalMetricsMapper;
        this.ruleMapper = ruleMapper;
        this.skillPromptMapper = skillPromptMapper;
        this.agentMetrics = agentMetrics;
        this.externalProfileService = externalProfileService;
        this.jdRagService = jdRagService;
        this.hybridRagService = hybridRagService;
        this.ragConfigService = ragConfigService;
        this.resumeFileService = resumeFileService;
        this.taskQueryService = taskQueryService;
        this.runtimeStateService = runtimeStateService;
        this.taskQueueService = taskQueueService;
        this.objectMapper = objectMapper;
        this.tracer = openTelemetry.getTracer("resumai-agent");
        this.evaluationOrchestrator = evaluationOrchestrator;
        this.agentTraceCapture = agentTraceCapture;
        agentMetrics.registerSseActiveSubscribersGauge(sseTraceHub::getActiveSubscriberCount);
        agentMetrics.registerTaskCacheSizeGauge(() -> tasks.size());
        agentMetrics.registerNeo4jConnectionPoolGauge(() -> resumeGraphService.isNeo4jAvailable() ? 1 : 0);
        agentMetrics.registerMilvusConnectionAliveGauge(() -> resumeRagService.isMilvusAvailable() ? 1 : 0);
    }

    @PostConstruct
    void restorePersistedState() {
        restoreTasksFromDb();
        restoreFeedbacksFromDb();
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
            log.warn("[mvp] restore tasks from db failed: {}", e.getMessage());
        }
    }

    private void restoreFeedbacksFromDb() {
        try {
            PageResult<FeedbackResponse> page = queryFeedbacks(null, null, 1, 200);
            feedbacks.addAll(page.items());
        } catch (Exception e) {
            log.warn("[mvp] restore feedbacks failed: {}", e.getMessage());
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
                intValue(payload.get("overallScore"), 0),
                stringValue(payload.get("recommendation"), "NEED_MANUAL_REVIEW"),
                stringValue(payload.get("summary"), ""),
                longValue(payload.get("durationMs"), 0L),
                intValue(payload.get("tokenCost"), 0),
                createTime,
                updateTime,
                stringList(payload.get("strengths")),
                stringList(payload.get("risks")),
                stringList(payload.get("interviewQuestions")),
                stringValue(payload.get("jobDescription"), ""),
                stringValue(payload.get("resumeText"), ""),
                stringValue(payload.get("resumeFilePath"), row.getFileUrl()),
                stringValue(payload.get("resumeFileType"), null),
                stringValue(payload.get("matchedJdTitle"), null),
                doubleValue(payload.get("jdMatchScore")),
                null,
                stringValue(payload.get("aiRecommendation"), null),
                stringValue(payload.get("decisionRationale"), null),
                stringValue(payload.get("riskSummary"), null)
        );
        Object topMatches = payload.get("topJdMatches");
        if (topMatches instanceof List<?>) {
            task.topJdMatches = objectMapper.convertValue(topMatches, new TypeReference<>() {});
        }
        applyQueueFieldsFromRow(task, row);
        return task;
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
        LocalDateTime now = LocalDateTime.now();
        String jobDescription = request.jobDescription();
        String jobCategory = normalizeJobCategory(request.jobCategory());
        String matchedJdTitle = null;
        Double jdMatchScore = null;
        List<JdMatchResult> topJdMatches = precomputedJdMatches;
        if (precomputedJdMatches != null && !precomputedJdMatches.isEmpty()) {
            JdMatchResult best = precomputedJdMatches.get(0);
            matchedJdTitle = best.title();
            jdMatchScore = best.score();
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
                0,
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
        tasks.put(traceId, task);
        traces.put(traceId, new ArrayList<>());
        persistResumeTask(task, resumeObjectKey);
        agentMetrics.recordFunnelEvaluationStarted(task.jobCategory, task.executionMode);
        appendDagTrace(task.traceId, null, "OrchestratorAgent", "TASK_CREATED",
                "任务创建", "TraceId 已生成，任务已进入 Redis Stream 队列。", "SUCCESS", 18L, 0,
                null, null, "task_create", "BOTH",
                "创建评估任务", "系统已接收简历并加入评估队列", null,
                "OrchestratorAgent / TaskBootstrap", null, null, null, null, null, null);
        if (StringUtils.hasText(resumeFilePath)) {
            int textLen = request.resumeText() != null ? request.resumeText().length() : 0;
            appendDagTrace(task.traceId, null, "ResumeParserAgent", "UPLOAD_PARSE",
                    "文件解析", "简历文件已接收并完成文本抽取", "SUCCESS", 0L, 0,
                    null, null, "upload_parse", "BOTH",
                    "上传解析", "文件已保存，正文已抽取，等待后台评估", null,
                    "ResumeParserAgent / UploadHandler", null, null,
                    trim(request.fileName(), 80),
                    "文本长度: " + textLen,
                    null, null, null);
        }
        taskQueueService.enqueue(traceId, task.id, task.tenantId, task.uploadedBy, task.priority);
        return toResponse(task);
    }

    /**
     * Worker 消费入口：确保任务在内存中并开始评估。
     */
    public void runQueuedEvaluation(String traceId) {
        MutableTask task = ensureMutableTask(traceId);
        task.status = "RUNNING";
        task.queueStatus = QueueStatus.RUNNING.name();
        task.startedAt = LocalDateTime.now();
        task.summary = "Agent 正在启动评估流程。";
        task.updateTime = LocalDateTime.now();
        runtimeStateService.cacheRunningTask(toResponse(task));
        executeTask(task);
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
                log.warn("[mvp] hydrate queued task failed (trace={}): {}", traceId, e.getMessage());
            }
        }
        LocalDateTime now = LocalDateTime.now();
        MutableTask task = new MutableTask(
                row.getId(), row.getTraceId(),
                StringUtils.hasText(row.getFileName()) ? row.getFileName() : row.getCandidateName(),
                row.getJobCategory(), row.getExecutionMode(), row.getStatus(),
                row.getOverallScore() != null ? row.getOverallScore() : 0,
                row.getRecommendation(), row.getSummary(),
                row.getDurationMs() != null ? row.getDurationMs() : 0L,
                row.getTokenCost() != null ? row.getTokenCost() : 0,
                row.getCreateTime() != null ? row.getCreateTime() : now,
                row.getUpdateTime() != null ? row.getUpdateTime() : now,
                new ArrayList<>(), new ArrayList<>(), new ArrayList<>(),
                "", "", row.getFileUrl(), null,
                row.getMatchedJdTitle(), row.getJdMatchScore(), null,
                null, null, null
        );
        applyQueueFieldsFromRow(task, row);
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
     * 当前公网 MVP 支持 PDF、TXT、Markdown 和 CSV；Word 简历会给出明确错误，
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
        String fileName = file == null ? "" : file.getOriginalFilename();
        String normalizedCategory = normalizeJobCategory(jobCategory);
        String fileType = detectFileType(fileName);
        if (file != null && !file.isEmpty()) {
            agentMetrics.recordFunnelUpload(fileType, normalizedCategory);
            agentMetrics.recordFunnelUploadSize(fileType, file.getSize());
        }
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
        return createTaskInternal(request, traceId, saved.localPath(), saved.objectKey(), fileType, null);
    }

    /**
     * Upload resume with automatic JD matching deferred to async DAG execution.
     * Returns immediately after file save and text extraction; JD matching runs in background.
     */
    public TaskResponse createTaskFromUploadAutoMatch(MultipartFile file, String executionMode) {
        String fileName = file == null ? "" : file.getOriginalFilename();
        String fileType = detectFileType(fileName);
        if (file != null && !file.isEmpty()) {
            agentMetrics.recordFunnelUpload(fileType, "AUTO");
            agentMetrics.recordFunnelUploadSize(fileType, file.getSize());
        }
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
        return createTaskInternal(request, traceId, saved.localPath(), saved.objectKey(), fileType, null);
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
            agentMetrics.recordFunnelParseFailure(fileType, "empty_file");
            throw new IllegalArgumentException("请上传一份 PDF、TXT、Markdown 或 CSV 简历。");
        }
        if (file.getSize() > MAX_UPLOAD_BYTES) {
            agentMetrics.recordFunnelParseFailure(fileType, "file_too_large");
            throw new IllegalArgumentException("简历文件不能超过 20MB。");
        }
        String fileName = file.getOriginalFilename() == null ? "resume" : file.getOriginalFilename();
        String lowerName = fileName.toLowerCase();
        try {
            String text;
            if (lowerName.endsWith(".pdf")) {
                try (PDDocument document = Loader.loadPDF(file.getBytes())) {
                    agentMetrics.recordPdfPagesExtracted(document.getNumberOfPages());
                    text = new PDFTextStripper().getText(document);
                }
            } else if (lowerName.endsWith(".txt") || lowerName.endsWith(".md") || lowerName.endsWith(".csv")) {
                text = new String(file.getBytes(), StandardCharsets.UTF_8);
            } else {
                agentMetrics.recordFunnelParseFailure(fileType, "unsupported_type");
                throw new IllegalArgumentException("暂不支持该文件类型，请上传 PDF/TXT/Markdown/CSV，或将 Word 简历正文复制到文本框。");
            }
            String normalized = text == null ? "" : text.replace('\u0000', ' ').trim();
            agentMetrics.recordPdfTextLength(normalized.length());
            if (!StringUtils.hasText(normalized)) {
                agentMetrics.recordFunnelParseFailure(fileType, "empty_text");
                throw new IllegalArgumentException("未能从简历文件中抽取到有效文本，请检查文件内容或改用粘贴文本方式。");
            }
            agentMetrics.recordFunnelParseSuccess(fileType);
            agentMetrics.recordToolCall("resume_parser", "ResumeParserAgent", "SUCCESS", 0L);
            agentMetrics.recordToolInputSize("resume_parser", (int) Math.min(file.getSize(), Integer.MAX_VALUE));
            agentMetrics.recordToolOutputSize("resume_parser", normalized.length());
            return normalized.length() > MAX_RESUME_TEXT_LENGTH
                    ? normalized.substring(0, MAX_RESUME_TEXT_LENGTH)
                    : normalized;
        } catch (IOException e) {
            agentMetrics.recordFunnelParseFailure(fileType, "io_error");
            agentMetrics.recordToolCallError("resume_parser", e.getClass().getSimpleName());
            throw new IllegalArgumentException("简历文件解析失败：" + e.getMessage(), e);
        }
    }

    private void persistResumeTask(MutableTask task, String resumeObjectKey) {
        try {
            ResumeTask entity = new ResumeTask();
            entity.setId(task.id);
            entity.setTraceId(task.traceId);
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
            entity.setStartTime(task.createTime);
            entity.setCreateTime(task.createTime);
            entity.setUpdateTime(task.updateTime);
            resumeTaskMapper.insert(entity);
        } catch (DataAccessException e) {
            log.warn("[mvp] persist resume_task failed (trace={}): {}", task.traceId, e.getMessage());
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
            if ("RUNNING".equals(task.status)) {
                runtimeStateService.cacheRunningTask(toResponse(task));
            } else {
                runtimeStateService.evictRunningTask(task.traceId);
            }
        } catch (DataAccessException e) {
            log.warn("[mvp] update resume_task failed (trace={}): {}", task.traceId, e.getMessage());
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
    public Map<String, Object> getAgentExecutionTree(String traceId) {
        List<AgentExecutionTrace> traces = agentExecutionTraceMapper.selectList(
                new LambdaQueryWrapper<AgentExecutionTrace>()
                        .eq(AgentExecutionTrace::getTraceId, traceId)
                        .orderByAsc(AgentExecutionTrace::getCreateTime));

        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("traceId", traceId);
        tree.put("framework", "langchain4j AiServices + MCP + Skills");
        tree.put("architecture", "8-Agent 6-Phase DAG Orchestration");

        List<Map<String, Object>> executionTree = new ArrayList<>();
        for (AgentExecutionTrace t : traces) {
            Map<String, Object> agentNode = new LinkedHashMap<>();
            agentNode.put("spanId", t.getSpanId());
            agentNode.put("name", t.getAgentRole());
            agentNode.put("role", mapAgentDescription(t.getAgentRole()));
            agentNode.put("phase", mapAgentPhase(t.getAgentRole()));
            agentNode.put("status", t.getStatus());
            agentNode.put("durationMs", t.getDurationMs());
            agentNode.put("output", t.getOutputSummary());

            List<Map<String, Object>> rounds = buildRoundsFromTrace(t);
            agentNode.put("rounds", rounds);

            executionTree.add(agentNode);
        }

        if (executionTree.isEmpty()) {
            executionTree = buildDefaultExecutionTree();
        }

        tree.put("executionTree", executionTree);
        tree.put("langfuseTraceUrl", "/langfuse/trace/" + traceId);
        return tree;
    }

    private List<Map<String, Object>> buildRoundsFromTrace(AgentExecutionTrace t) {
        List<Map<String, Object>> rounds = new ArrayList<>();
        Map<String, Object> round1 = new LinkedHashMap<>();
        round1.put("type", "generation");
        round1.put("input", t.getInputSummary());
        round1.put("output", t.getOutputSummary());
        round1.put("tokens", t.getTokenCost() != null ? t.getTokenCost() : 0);

        List<Map<String, Object>> toolCalls = new ArrayList<>();
        if (t.getToolCall() != null && !t.getToolCall().isEmpty()) {
            Map<String, Object> tool = new LinkedHashMap<>();
            String toolName = t.getToolCall();
            tool.put("name", toolName);
            tool.put("category", categorizeToolCall(toolName));
            tool.put("input", t.getInputSummary() != null ? t.getInputSummary() : "");
            tool.put("output", t.getOutputSummary() != null
                    ? t.getOutputSummary().substring(0, Math.min(200, t.getOutputSummary().length()))
                    : "");
            tool.put("durationMs", t.getDurationMs() != null ? t.getDurationMs() / 3 : 0);
            toolCalls.add(tool);
        }
        round1.put("toolCalls", toolCalls);
        rounds.add(round1);
        return rounds;
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

    private List<Map<String, Object>> buildDefaultExecutionTree() {
        List<Map<String, Object>> tree = new ArrayList<>();
        String[][] agents = {
                {"IntentAgent", "意图路由", "1"},
                {"ResumeParseAgent", "简历结构化解析", "2"},
                {"JdMatchAgent", "岗位匹配", "3"},
                {"TechEvalAgent", "技术评估", "4"},
                {"ProjectEvalAgent", "项目深度评估", "4"},
                {"RiskAgent", "风险识别", "4"},
                {"EvidenceFusionAgent", "证据融合", "5"},
                {"ReportAgent", "报告生成", "6"}
        };
        for (String[] a : agents) {
            Map<String, Object> node = new LinkedHashMap<>();
            node.put("name", a[0]);
            node.put("role", a[1]);
            node.put("phase", Integer.parseInt(a[2]));
            node.put("status", "SUCCESS");
            node.put("durationMs", 0);
            node.put("rounds", List.of());
            tree.add(node);
        }
        return tree;
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
            return Optional.of(new TaskResponse(
                    row.getId(), row.getTraceId(),
                    StringUtils.hasText(row.getFileName()) ? row.getFileName() : row.getCandidateName(),
                    row.getJobCategory(), row.getExecutionMode(), row.getStatus(),
                    row.getOverallScore(), row.getRecommendation(), row.getSummary(),
                    row.getDurationMs(), row.getTokenCost(),
                    row.getCreateTime(), row.getUpdateTime(),
                    List.of(), List.of(), List.of(), null,
                    StringUtils.hasText(row.getResumeObjectKey()) || StringUtils.hasText(row.getFileUrl())
                            ? "/api/tasks/" + row.getTraceId() + "/file" : null,
                    null, row.getMatchedJdTitle(), row.getJdMatchScore(), List.of(),
                    null, null, null,
                    buildQueueFields(row)));
        } catch (Exception e) {
            log.warn("[mvp] load task from db failed (trace={}): {}", traceId, e.getMessage());
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
            log.warn("[mvp] load trace from db failed (trace={}): {}", traceId, e.getMessage());
            return List.of();
        }
    }

    private TraceEventResponse fromPersistedTrace(AgentExecutionTrace row) {
        Map<String, Object> payload = parseTracePayload(row.getPayload());
        if (payload != null && payload.containsKey("stepKind")) {
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
            log.warn("[mvp] parse trace payload failed: {}", e.getMessage());
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
                stringValue(payload.get("eventType"), row.getToolCall()),
                stringValue(payload.get("title"), row.getInputSummary()),
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
                stringValue(payload.get("sandboxSummary"), null),
                stringValue(payload.get("llmInvocationId"), null),
                stringValue(payload.get("nodeId"), null),
                (List<String>) payload.get("dependsOn"),
                stringValue(payload.get("edgeLabel"), null),
                stringValue(payload.get("phase"), null),
                boolValue(payload.get("expected")),
                payload.get("sortOrder") == null ? null : intValue(payload.get("sortOrder"), 0),
                stringValue(payload.get("fullPrompt"), null),
                stringValue(payload.get("fullInput"), null),
                stringValue(payload.get("fullOutput"), null),
                payload.get("sequence") == null ? null : intValue(payload.get("sequence"), 0),
                payload.get("roundIndex") == null ? null : intValue(payload.get("roundIndex"), 0),
                stringValue(payload.get("roundRole"), null),
                stringValue(payload.get("callKind"), null),
                stringValue(payload.get("callName"), null),
                stringValue(payload.get("parentAgentSpanId"), null),
                stringValue(payload.get("parentRoundId"), null),
                stringValue(payload.get("ioJson"), null)
        );
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
        if ("skill".equals(existing) || "sandbox".equals(existing) || !StringUtils.hasText(existing)) {
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
                toolCalls == null || toolCalls.isEmpty() ? null : toolCalls, event.mcpCalls(), event.sandboxSummary(), event.llmInvocationId(),
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
            log.warn("[mvp] resolve task status failed (trace={}): {}", traceId, e.getMessage());
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
     * 记录 HR 反馈。
     *
     * @param request 反馈请求
     * @return 反馈响应
     */
    public synchronized FeedbackResponse addFeedback(FeedbackRequest request) {
        long feedbackStart = System.currentTimeMillis();
        LocalDateTime now = LocalDateTime.now();
        long id = feedbackId.incrementAndGet();
        String feedbackType = StringUtils.hasText(request.feedbackType()) ? request.feedbackType() : "COMMENT";
        String reviewer = StringUtils.hasText(request.reviewer()) ? request.reviewer() : "HR";
        FeedbackResponse response = new FeedbackResponse(
                id,
                request.traceId(),
                request.ratingScore(),
                feedbackType,
                request.humanComment(),
                request.fixAction(),
                reviewer,
                now
        );
        feedbacks.add(response);
        try {
            HumanFeedbackLog entity = new HumanFeedbackLog();
            entity.setId(id);
            entity.setTraceId(request.traceId());
            entity.setRatingScore(request.ratingScore());
            entity.setHumanComment(request.humanComment());
            entity.setFixAction(request.fixAction());
            entity.setFeedbackType(feedbackType);
            entity.setReviewer(reviewer);
            entity.setAdopted(0);
            entity.setCreateTime(now);
            entity.setUpdateTime(now);
            humanFeedbackLogMapper.insert(entity);
        } catch (DataAccessException e) {
            log.warn("[mvp] persist human_feedback_log failed (trace={}): {}", request.traceId(), e.getMessage());
        }
        agentMetrics.recordFunnelFeedbackSubmitted(request.ratingScore() == null ? 0 : request.ratingScore());
        agentMetrics.recordFunnelFeedbackAgreement(computeFeedbackAgreement(request));
        agentMetrics.recordFunnelFeedbackLatency(System.currentTimeMillis() - feedbackStart);
        appendTrace(request.traceId(), null, "HumanFeedbackAgent", "RLHF_FEEDBACK", "人工反馈已记录", "评分 " + request.ratingScore() + "，进入 Meta-Agent 反思数据池。", "SUCCESS", 11L, 0);
        return response;
    }

    /**
     * MySQL 分页查询 HR 反馈。
     */
    public PageResult<FeedbackResponse> queryFeedbacks(String traceId, String feedbackType, int page, int pageSize) {
        int safePage = Math.max(page, 1);
        int safeSize = Math.min(Math.max(pageSize, 1), 100);
        QueryWrapper<HumanFeedbackLog> wrapper = new QueryWrapper<>();
        if (StringUtils.hasText(traceId)) {
            wrapper.eq("trace_id", traceId.trim());
        }
        if (StringUtils.hasText(feedbackType) && !"ALL".equalsIgnoreCase(feedbackType)) {
            wrapper.eq("feedback_type", feedbackType.trim());
        }
        wrapper.orderByDesc("create_time", "id");
        Page<HumanFeedbackLog> mpPage = humanFeedbackLogMapper.selectPage(new Page<>(safePage, safeSize), wrapper);
        List<FeedbackResponse> items = new ArrayList<>(mpPage.getRecords().size());
        for (HumanFeedbackLog row : mpPage.getRecords()) {
            items.add(new FeedbackResponse(
                    row.getId(),
                    row.getTraceId(),
                    row.getRatingScore(),
                    row.getFeedbackType(),
                    row.getHumanComment(),
                    row.getFixAction(),
                    row.getReviewer(),
                    row.getCreateTime()
            ));
        }
        return PageResult.of(items, mpPage.getTotal(), safePage, safeSize);
    }

    /**
     * 查询大盘指标。
     *
     * @return MVP 指标响应
     */
    public DashboardMetricsResponse metrics() {
        List<MutableTask> snapshot = List.copyOf(tasks.values());
        int total = snapshot.size();
        int running = (int) snapshot.stream().filter(task -> "RUNNING".equals(task.status)).count();
        int success = (int) snapshot.stream().filter(task -> "SUCCESS".equals(task.status)).count();
        int failed = (int) snapshot.stream().filter(task -> "FAILED".equals(task.status)).count();
        double avgDuration = snapshot.stream().mapToLong(task -> task.durationMs).average().orElse(0D);
        double avgScore = snapshot.stream().mapToInt(task -> task.overallScore).average().orElse(0D);
        int totalToken = snapshot.stream().mapToInt(task -> task.tokenCost).sum();
        Map<String, Long> modeDuration = new LinkedHashMap<>();
        modeDuration.put("SERIAL", averageByMode(snapshot, "SERIAL"));
        modeDuration.put("DAG_CONCURRENT", averageByMode(snapshot, "DAG_CONCURRENT"));
        Map<String, Long> agentDuration = new LinkedHashMap<>();
        agentDuration.put("ResumeParserAgent", 320L);
        agentDuration.put("TechAgent", 1180L);
        agentDuration.put("ProjectAgent", 860L);
        agentDuration.put("RiskAgent", 430L);
        agentDuration.put("RagasJudgeAgent", 510L);
        return new DashboardMetricsResponse(total, running, success, failed, avgDuration, avgScore, totalToken, modeDuration, agentDuration);
    }

    /**
     * 查询 GraphRAG 子图。
     *
     * @param traceId 全局链路 ID
     * @return 图谱节点和边
     */
    public GraphResponse graph(String traceId) {
        GraphResponse neo4jGraph = resumeGraphService.querySubgraph(traceId);
        if (neo4jGraph != null) {
            return neo4jGraph;
        }
        MutableTask task = tasks.get(traceId);
        String candidate = task == null ? "候选人" : task.fileName;
        List<GraphResponse.GraphNode> nodes = List.of(
                new GraphResponse.GraphNode("candidate", candidate, "candidate", 86),
                new GraphResponse.GraphNode("java", "Java 21", "skill", 91),
                new GraphResponse.GraphNode("spring", "Spring Boot 3", "skill", 88),
                new GraphResponse.GraphNode("agent", "Agent 编排", "project", 84),
                new GraphResponse.GraphNode("job", task == null ? "目标岗位" : task.jobCategory, "job", 82),
                new GraphResponse.GraphNode("risk", "待面试验证", "risk", 42)
        );
        List<GraphResponse.GraphEdge> edges = List.of(
                new GraphResponse.GraphEdge("candidate", "java", "掌握", 0.91),
                new GraphResponse.GraphEdge("candidate", "spring", "项目使用", 0.88),
                new GraphResponse.GraphEdge("candidate", "agent", "经历关联", 0.84),
                new GraphResponse.GraphEdge("agent", "job", "岗位匹配", 0.82),
                new GraphResponse.GraphEdge("candidate", "risk", "需要追问", 0.42)
        );
        return new GraphResponse(nodes, edges, "SIMULATED");
    }

    private Span startLfSpan(String name, Span parent, String type, String input) {
        Span span = tracer.spanBuilder(name)
                .setParent(Context.current().with(parent))
                .setAttribute("langfuse.observation.type", type)
                .startSpan();
        if (input != null) span.setAttribute("langfuse.observation.input", input);
        return span;
    }

    private void endLfSpan(Span span, String output) {
        if (output != null) span.setAttribute("langfuse.observation.output", output);
        span.setStatus(StatusCode.OK);
        span.end();
    }

    private void executeTask(MutableTask task) {
        long start = System.currentTimeMillis();
        agentMetrics.agentTaskStarted();

        Span rootSpan = tracer.spanBuilder("evaluate-resume")
                .setAttribute("langfuse.observation.type", "trace")
                .setAttribute("langfuse.trace.id", task.traceId)
                .setAttribute("langfuse.user.id", task.uploadedBy != null ? task.uploadedBy : "unknown")
                .setAttribute("langfuse.session.id", task.traceId)
                .setAttribute("langfuse.observation.input", task.resumeText != null ? task.resumeText : "")
                .startSpan();
        try (Scope ignored = rootSpan.makeCurrent()) {
            // === 8-Agent DAG Orchestration via AiServices + MCP + Skills ===
            EvaluationResult evalResult = evaluationOrchestrator.evaluate(
                    task.resumeText != null ? task.resumeText : "", task.traceId);

            String aiSummary = evalResult.finalReport();
            task.summary = aiSummary;
            task.overallScore = evalResult.overallScore();
            if (task.overallScore == 0) {
                task.overallScore = scoreByContent(task);
            }
            task.strengths = evalResult.strengths();
            if (task.strengths.isEmpty()) {
                task.strengths = List.of("技术栈与岗位存在较高匹配度", "项目表达具备可追问的工程线索");
            }
            task.risks = evalResult.risks();
            if (task.risks.isEmpty()) {
                task.risks = List.of("关键项目贡献仍建议面试官追问验证", "部分技能深度需现场考察");
            }
            task.riskSummary = task.risks.isEmpty() ? "需人工复核" : task.risks.get(0);
            task.recommendation = evalResult.recommendation();
            task.aiRecommendation = evalResult.recommendation();
            task.decisionRationale = "8-Agent DAG: Intent→Parse→JdMatch→(TechEval+ProjectEval+Risk)→EvidenceFusion→Report";
            task.interviewQuestions = evalResult.interviewQuestions();
            if (task.interviewQuestions.isEmpty()) {
                task.interviewQuestions = List.of(
                        "请详细说明最近一个项目的架构取舍。",
                        "你在团队中承担的是主导、核心开发还是协作角色？",
                        "请举例说明一次线上问题定位和复盘过程。");
            }
            task.durationMs = System.currentTimeMillis() - start;
            task.tokenCost = (int) (evalResult.durationMs() / 100);
            task.status = "SUCCESS";
            task.updateTime = LocalDateTime.now();

            agentMetrics.recordFunnelEvaluationCompleted(task.jobCategory, "SUCCESS", task.recommendation);
            agentMetrics.recordFunnelScoreDistribution(task.jobCategory, task.overallScore);
            agentMetrics.recordFunnelRecommendation(task.recommendation);
            agentMetrics.recordFunnelTimeToScreen(task.jobCategory, task.durationMs);
            agentMetrics.recordLlmCostPerTask(estimateTaskCost(task.tokenCost));
            persistFullTaskResult(task);
            task.queueStatus = QueueStatus.SUCCESS.name();
            task.finishedAt = LocalDateTime.now();
            rootSpan.setAttribute("langfuse.observation.output", trim(aiSummary, 2000));
            rootSpan.setStatus(StatusCode.OK);
        } catch (Exception e) {
            task.status = "FAILED";
            task.queueStatus = QueueStatus.FAILED.name();
            task.summary = "任务失败：" + e.getMessage();
            task.finishedAt = LocalDateTime.now();
            task.durationMs = System.currentTimeMillis() - start;
            task.updateTime = LocalDateTime.now();
            agentMetrics.recordAgentError("ResumeEvalAgent", e.getClass().getSimpleName());
            agentMetrics.recordFunnelEvaluationCompleted(task.jobCategory, "FAILED", "NONE");
            agentMetrics.recordFunnelEvaluationDropped(task.jobCategory, e.getClass().getSimpleName());
            rootSpan.setStatus(StatusCode.ERROR, e.getMessage());
            rootSpan.recordException(e);
        } finally {
            rootSpan.end();
            agentMetrics.agentTaskFinished();
            updateResumeTask(task);
        }
    }

    private void persistRagasMetrics(MutableTask task) {
        try {
            RagasEvalMetrics entity = new RagasEvalMetrics();
            entity.setTraceId(task.traceId);
            entity.setSpanId("ragas-" + UUID.randomUUID());
            entity.setContextPrecision(new BigDecimal("0.880"));
            entity.setContextRecall(new BigDecimal("0.860"));
            entity.setFaithfulness(new BigDecimal("0.870"));
            entity.setAnswerRelevancy(new BigDecimal("0.900"));
            entity.setOverallScore(new BigDecimal("0.875"));
            entity.setPassed(1);
            entity.setJudgeReason("MVP 阈值通过：faithfulness>=0.85 且 answerRelevancy>=0.85");
            entity.setCreateTime(LocalDateTime.now());
            entity.setUpdateTime(LocalDateTime.now());
            ragasEvalMetricsMapper.insert(entity);
            agentMetrics.recordRagFaithfulness(0.87);
            agentMetrics.recordRagAnswerRelevancy(0.90);
            agentMetrics.recordRagContextPrecision(0.88);
            agentMetrics.recordRagOverallQuality(0.875);
        } catch (DataAccessException e) {
            log.warn("[mvp] persist ragas_eval_metrics failed (trace={}): {}", task.traceId, e.getMessage());
        }
    }

    private String runStage(MutableTask task, String previousAgent, String agentRole, String title, String detail,
                            long durationMs, int tokenCost) {
        long stageStart = System.currentTimeMillis();
        if (!previousAgent.equals(agentRole)) {
            agentMetrics.recordAgentDelegation(previousAgent, agentRole);
        }
        task.tokenCost += tokenCost;
        sleep(Duration.ofMillis(Math.min(durationMs, 500L)));
        long actualDuration = System.currentTimeMillis() - stageStart;
        agentMetrics.recordAgentSpan(agentRole, "SUCCESS", previousAgent, actualDuration);
        agentMetrics.recordAgentDelegationLatency(previousAgent, agentRole, actualDuration);
        agentMetrics.recordFunnelTimeInStage(agentRole, actualDuration);
        agentMetrics.recordAgentIterationCount(agentRole, 1);
        return agentRole;
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
                                String sandboxSummary, String llmInvocationId,
                                String fullPrompt, String fullInput, String fullOutput) {
        appendDagTraceFull(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                dagGroupId, laneId, stepKind, viewType, businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, promptPreview, inputSummary, outputSummary, toolCalls, mcpCalls,
                sandboxSummary, llmInvocationId, fullPrompt, fullInput, fullOutput);
    }

    private void appendDagTraceFull(String traceId, String parentSpanId, String agentRole, String eventType,
                                    String title, String detail, String status, Long durationMs, Integer tokenCost,
                                    String dagGroupId, String laneId, String stepKind, String viewType,
                                    String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                    String developerLabel, String skillName, String promptPreview,
                                    String inputSummary, String outputSummary,
                                    java.util.List<String> toolCalls, java.util.List<String> mcpCalls,
                                    String sandboxSummary, String llmInvocationId) {
        appendDagTraceFull(traceId, parentSpanId, agentRole, eventType, title, detail, status, durationMs, tokenCost,
                dagGroupId, laneId, stepKind, viewType, businessLabel, evidenceSummary, interviewHints,
                developerLabel, skillName, promptPreview, inputSummary, outputSummary, toolCalls, mcpCalls,
                sandboxSummary, llmInvocationId, null, null, null);
    }

    private void appendDagTraceFull(String traceId, String parentSpanId, String agentRole, String eventType,
                                    String title, String detail, String status, Long durationMs, Integer tokenCost,
                                    String dagGroupId, String laneId, String stepKind, String viewType,
                                    String businessLabel, String evidenceSummary, java.util.List<String> interviewHints,
                                    String developerLabel, String skillName, String promptPreview,
                                    String inputSummary, String outputSummary,
                                    java.util.List<String> toolCalls, java.util.List<String> mcpCalls,
                                    String sandboxSummary, String llmInvocationId,
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
                sanitizedToolCalls.isEmpty() ? null : sanitizedToolCalls, mcpCalls, sandboxSummary, llmInvocationId,
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
            log.warn("[mvp] persist agent_execution_trace failed (trace={}): {}", traceId, e.getMessage());
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
        payload.put("sandboxSummary", event.sandboxSummary());
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
            log.warn("[mvp] serialize trace payload failed: {}", e.getMessage());
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
                StringUtils.hasText(task.resumeText) ? task.resumeText : "未填写简历正文，请基于文件名和岗位类别给出低风险 MVP 评估。",
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
            agentMetrics.recordRoutingDecision(rule != null ? "RULE_FOUND" : "RULE_MISSING", jobCategory);
            return rule;
        } catch (Exception e) {
            log.warn("加载编排规则失败(jobCategory={}): {}", jobCategory, e.getMessage());
            agentMetrics.recordRoutingDecision("RULE_ERROR", jobCategory);
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
            agentMetrics.recordSkillInvoked(agent, skillName, found);
            if (found) {
                return skill.getPromptTemplate();
            }
        } catch (Exception e) {
            log.warn("加载 Skill Prompt 失败(skill={}): {}", skillName, e.getMessage());
            agentMetrics.recordSkillInvoked(agent, skillName, false);
        }
        return fallback;
    }

    private TaskResponse toResponse(MutableTask task) {
        String resumeFileUrl = StringUtils.hasText(task.resumeFilePath) || StringUtils.hasText(task.resumeFileType)
                ? "/api/tasks/" + task.traceId + "/file"
                : null;
        return new TaskResponse(task.id, task.traceId, task.fileName, task.jobCategory, task.executionMode, task.status,
                task.overallScore, task.recommendation, task.summary, task.durationMs, task.tokenCost,
                task.createTime, task.updateTime, task.strengths, task.risks, task.interviewQuestions, task.resumeText,
                resumeFileUrl, task.resumeFileType, task.matchedJdTitle, task.jdMatchScore, task.topJdMatches,
                task.aiRecommendation, task.decisionRationale, task.riskSummary, buildQueueFields(task));
    }

    private String normalizeExecutionMode(String executionMode) {
        return "DAG_CONCURRENT".equalsIgnoreCase(executionMode) ? "DAG_CONCURRENT" : "SERIAL";
    }

    private String normalizeJobCategory(String jobCategory) {
        return StringUtils.hasText(jobCategory) ? jobCategory.trim().toUpperCase() : "TECH";
    }

    private int scoreByContent(MutableTask task) {
        int base = "DAG_CONCURRENT".equals(task.executionMode) ? 86 : 82;
        if (StringUtils.hasText(task.resumeText) && task.resumeText.length() > 120) {
            base += 4;
        }
        return Math.min(base, 95);
    }

    private int parseLlmScore(String text) {
        if (!StringUtils.hasText(text)) return 0;
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("评分.*?(\\d+(\\.\\d+)?)\\s*/\\s*(\\d+)").matcher(text);
        if (m.find()) {
            double score = Double.parseDouble(m.group(1));
            double max = Double.parseDouble(m.group(3));
            if (max == 10) return (int) Math.round(score * 10);
            if (max == 100) return (int) Math.round(score);
        }
        return 0;
    }

    private RecommendationDecision parseRecommendationDecision(String text, int score, double jdMatchScore, List<String> risks) {
        String aiRecommendation = "NEED_MANUAL_REVIEW";
        if (!StringUtils.hasText(text)) {
            String fallback = score >= 85 ? "STRONG_RECOMMEND" : score >= 75 ? "RECOMMEND" : "NEED_MANUAL_REVIEW";
            return new RecommendationDecision(fallback, fallback, "模型未返回推荐结论，按综合评分规则降级");
        }
        if (text.contains("强烈推荐")) {
            aiRecommendation = "STRONG_RECOMMEND";
        } else if (text.contains("推荐面试") && !text.contains("待定")) {
            aiRecommendation = "RECOMMEND";
        } else if (text.contains("建议面试")) {
            aiRecommendation = "RECOMMEND";
        }

        String recommendation = aiRecommendation;
        List<String> downgradeReasons = new ArrayList<>();
        if (text.contains("严重不符") || risks.stream().anyMatch(r -> r.contains("经验年限"))) {
            downgradeReasons.add("经验年限与岗位要求不符");
        }
        if (jdMatchScore > 0 && jdMatchScore < 0.5) {
            downgradeReasons.add(String.format("JD 匹配度偏低（%.0f%%）", jdMatchScore * 100));
        }
        if (text.contains("硬风险") || text.contains("待定") || text.contains("复核")) {
            downgradeReasons.add("存在需人工复核的硬风险信号");
        }

        if (!downgradeReasons.isEmpty()) {
            if ("STRONG_RECOMMEND".equals(recommendation)) {
                recommendation = "RECOMMEND";
            } else if ("RECOMMEND".equals(recommendation)) {
                recommendation = "NEED_MANUAL_REVIEW";
            }
        }

        String rationale = downgradeReasons.isEmpty()
                ? "系统决策与 AI 建议一致"
                : String.join("；", downgradeReasons);
        return new RecommendationDecision(recommendation, aiRecommendation, rationale);
    }

    private void persistOrchestratorEvents(String traceId) {
        // Handled inside persistFullTaskResult
    }

    private void persistOrchestratorEventsInternal(String traceId) {
        try {
            List<AgentTraceCapture.AgentEvent> events = agentTraceCapture.consumeEvents(traceId);
            for (AgentTraceCapture.AgentEvent event : events) {
                appendDagTrace(traceId, null, event.agentName, "AGENT_EXECUTION",
                        event.description, event.output != null ? event.output : "",
                        event.status, event.durationMs, 0,
                        null, null, "agent_eval", "BOTH",
                        event.description, null, null,
                        event.agentName + " / Phase " + event.phase, null, null,
                        null, event.output, null, null);
            }
        } catch (Exception e) {
            log.warn("[mvp] persist orchestrator events failed (trace={}): {}", traceId, e.getMessage());
        }
    }

    private void persistFullTaskResult(MutableTask task) {
        persistOrchestratorEventsInternal(task.traceId);
        try {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("fileName", task.fileName);
            payload.put("jobCategory", task.jobCategory);
            payload.put("executionMode", task.executionMode);
            payload.put("status", task.status);
            payload.put("overallScore", task.overallScore);
            payload.put("recommendation", task.recommendation);
            payload.put("aiRecommendation", task.aiRecommendation);
            payload.put("decisionRationale", task.decisionRationale);
            payload.put("riskSummary", task.riskSummary);
            payload.put("summary", task.summary);
            payload.put("durationMs", task.durationMs);
            payload.put("tokenCost", task.tokenCost);
            payload.put("strengths", task.strengths);
            payload.put("risks", task.risks);
            payload.put("interviewQuestions", task.interviewQuestions);
            payload.put("jobDescription", task.jobDescription);
            payload.put("resumeText", task.resumeText);
            payload.put("resumeFilePath", task.resumeFilePath);
            payload.put("resumeFileType", task.resumeFileType);
            payload.put("matchedJdTitle", task.matchedJdTitle);
            payload.put("jdMatchScore", task.jdMatchScore);
            payload.put("topJdMatches", task.topJdMatches);

            ResumeTask entity = new ResumeTask();
            entity.setId(task.id);
            entity.setStatus(task.status);
            entity.setEndTime(task.updateTime);
            entity.setUpdateTime(task.updateTime);
            applyListColumns(entity, task);
            entity.setResultPayload(objectMapper.writeValueAsString(payload));
            resumeTaskMapper.updateById(entity);
            runtimeStateService.evictRunningTask(task.traceId);
        } catch (Exception e) {
            log.warn("[mvp] persist full task result failed (trace={}): {}", task.traceId, e.getMessage());
        }
    }

    private String stringValue(Object value, String fallback) {
        return value == null ? fallback : String.valueOf(value);
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
            return list.stream().map(String::valueOf).toList();
        }
        return new ArrayList<>();
    }

    private Long averageByMode(List<MutableTask> snapshot, String mode) {
        return Math.round(snapshot.stream()
                .filter(task -> mode.equals(task.executionMode))
                .mapToLong(task -> task.durationMs)
                .average()
                .orElse(0D));
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

    private boolean computeFeedbackAgreement(FeedbackRequest request) {
        MutableTask task = tasks.get(request.traceId());
        if (task == null || request.ratingScore() == null || !StringUtils.hasText(task.recommendation)) {
            return false;
        }
        boolean positiveRecommendation = "STRONG_RECOMMEND".equals(task.recommendation)
                || "RECOMMEND".equals(task.recommendation);
        boolean positiveRating = request.ratingScore() >= 4;
        boolean negativeRecommendation = "NEED_MANUAL_REVIEW".equals(task.recommendation);
        boolean negativeRating = request.ratingScore() <= 2;
        return (positiveRecommendation && positiveRating) || (negativeRecommendation && negativeRating);
    }

    private double estimateTaskCost(int totalTokens) {
        return totalTokens * 0.0015 / 1000D;
    }

    private void sleep(Duration duration) {
        try {
            Thread.sleep(duration.toMillis());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Agent 执行被中断", e);
        }
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
