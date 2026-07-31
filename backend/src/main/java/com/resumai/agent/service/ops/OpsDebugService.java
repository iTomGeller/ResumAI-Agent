package com.resumai.agent.service.ops;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.ArtifactDebugView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.BudgetDebugView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.CorrelationView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.ErrorDiagnosticView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.EventOutcome;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpInventory;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpInventoryServer;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpEndpointStats;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpInvocationPage;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpInvocationView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryTtlView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryUsageView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.PlanDebugView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagChunkView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagOpsSummary;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagQualityView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagRetrievalView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagStageAggregateView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagStageTimingView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugDetailResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugSummary;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillAggUsage;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillManifestItem;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillUsageView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.TimelineEventView;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.MemoryEntryMapper;
import com.resumai.agent.dao.RunEventMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import com.resumai.agent.domain.entity.RunEvent;
import com.resumai.agent.domain.entity.RunMemoryUsageRow;
import com.resumai.agent.service.AgentMemoryService;
import com.resumai.agent.service.MemoryService;
import com.resumai.agent.service.RunMemoryUsageService;
import com.resumai.agent.service.run.AgentRuntimeClient;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Shared builder for run-centric Ops / Dev debug APIs.
 */
@Service
public class OpsDebugService {

    public static final List<String> SKILL_EVENT_TYPES = List.of(
            "skill.catalog", "skill.catalog.exposed", "skill.selected",
            "skill.loaded", "skill.applied", "skill.skipped", "skill.failed",
            // Legacy aliases for historical rows only.
            "skill.started", "skill.completed");

    public static final List<String> TOOL_EVENT_TYPES = List.of(
            "tool.started", "tool.completed", "tool.failed");

    public static final List<String> MCP_EVENT_TYPES = List.of(
            "tool.started", "tool.progress", "tool.completed", "tool.failed");

    public static final List<String> RETRIEVAL_EVENT_TYPES = List.of(
            "retrieval.started", "retrieval.completed", "retrieval.failed");

    public static final Set<String> RETRIEVAL_TOOL_NAMES = Set.of(
            "knowledge_search", "resume_semantic_search", "jd_match_search");

    private static final Set<String> BUSINESS_ARTIFACT_KEYS = Set.of(
            "resumeFacts", "parsedResume", "technicalFindings", "finalReport",
            "jdCoverage", "jdMatches", "evidence", "risks", "conflicts",
            "effectiveJd", "structuredReport");

    private final AgentRunMapper agentRunMapper;
    private final RunEventMapper runEventMapper;
    private final MemoryEntryMapper memoryEntryMapper;
    private final RunMemoryUsageService memoryUsageService;
    private final AgentMemoryService agentMemoryService;
    private final AgentRuntimeClient runtimeClient;
    private final ObjectMapper objectMapper;

    public OpsDebugService(AgentRunMapper agentRunMapper,
                           RunEventMapper runEventMapper,
                           MemoryEntryMapper memoryEntryMapper,
                           RunMemoryUsageService memoryUsageService,
                           AgentMemoryService agentMemoryService,
                           AgentRuntimeClient runtimeClient,
                           ObjectMapper objectMapper) {
        this.agentRunMapper = agentRunMapper;
        this.runEventMapper = runEventMapper;
        this.memoryEntryMapper = memoryEntryMapper;
        this.memoryUsageService = memoryUsageService;
        this.agentMemoryService = agentMemoryService;
        this.runtimeClient = runtimeClient;
        this.objectMapper = objectMapper;
    }

    public EventOutcome deriveOutcome(String eventType) {
        if (!StringUtils.hasText(eventType)) {
            return EventOutcome.INFO;
        }
        String type = eventType.trim();
        if (type.endsWith(".failed") || "run.failed".equals(type) || "run.timed_out".equals(type)
                || type.contains("timed_out")) {
            return EventOutcome.FAILED;
        }
        if (type.endsWith(".completed") || "run.completed".equals(type)
                || "skill.completed".equals(type)) {
            return EventOutcome.SUCCESS;
        }
        if ("skill.selected".equals(type) || "skill.applied".equals(type)) {
            return EventOutcome.INFO;
        }
        if (type.endsWith(".started") || type.endsWith(".progress") || type.endsWith(".retrying")) {
            return EventOutcome.RUNNING;
        }
        return EventOutcome.INFO;
    }

    public List<RunDebugSummary> listRuns(String traceId, String runId, String conversationId,
                                          String status, int limit) {
        int cap = Math.max(1, Math.min(limit, 200));
        QueryWrapper<AgentRun> q = new QueryWrapper<AgentRun>().orderByDesc("created_at");
        if (StringUtils.hasText(runId)) {
            q.eq("run_id", runId.trim());
        }
        if (StringUtils.hasText(traceId)) {
            q.and(w -> w.eq("trace_id", traceId.trim())
                    .or().eq("source_task_trace_id", traceId.trim()));
        }
        if (StringUtils.hasText(conversationId)) {
            q.eq("conversation_id", conversationId.trim());
        }
        if (StringUtils.hasText(status)) {
            q.eq("status", status.trim().toUpperCase(Locale.ROOT));
        }
        q.last("limit " + cap);
        return agentRunMapper.selectList(q).stream().map(this::toSummary).toList();
    }

    public RunDebugDetailResponse runDetail(String runId, int eventLimit, Long afterSeq) {
        AgentRun run = agentRunMapper.selectById(runId);
        if (run == null) {
            return null;
        }
        int cap = Math.max(1, Math.min(eventLimit, 500));
        QueryWrapper<RunEvent> q = new QueryWrapper<RunEvent>()
                .eq("run_id", runId)
                .orderByAsc("seq")
                .last("limit " + cap);
        if (afterSeq != null && afterSeq > 0) {
            q.gt("seq", afterSeq);
        }
        List<RunEvent> events = runEventMapper.selectList(q);

        List<TimelineEventView> timeline = new ArrayList<>();
        List<ErrorDiagnosticView> errors = new ArrayList<>();
        List<McpInvocationView> mcpCalls = new ArrayList<>();
        List<SkillUsageView> skills = new ArrayList<>();
        PlanDebugView plan = PlanDebugView.empty();
        Integer nextSeq = null;
        boolean truncated = events.size() >= cap;

        for (RunEvent event : events) {
            Object payload = parseJson(event.getPayload());
            EventOutcome outcome = deriveOutcome(event.getEventType());
            timeline.add(new TimelineEventView(
                    event.getSeq(), event.getEventType(), event.getAgentId(),
                    event.getToolName(), outcome, payload, event.getCreateTime()));
            if (event.getSeq() != null) {
                nextSeq = event.getSeq();
            }
            String type = event.getEventType() == null ? "" : event.getEventType();
            if (SKILL_EVENT_TYPES.contains(type)) {
                skills.add(toSkillUsage(event, payload));
            }
            if (MCP_EVENT_TYPES.contains(type) && isMcpPayload(payload)) {
                mcpCalls.add(toMcpInvocation(event, payload));
            }
            if (type.contains("failed") || type.contains("error") || "run.failed".equals(type)
                    || "run.timed_out".equals(type)) {
                errors.add(toError(event, payload, false));
            }
            if ("agent.selected".equals(type) && "CoordinatorAgent".equals(event.getAgentId())) {
                plan = planFromPayload(payload);
            }
        }
        if (!errors.isEmpty()) {
            ErrorDiagnosticView first = errors.get(0);
            errors.set(0, new ErrorDiagnosticView(
                    first.seq(), first.eventType(), first.agentId(), first.toolName(),
                    first.errorCode(), first.message(), first.payload(), first.createTime(), true));
        }

        Object shared = parseJson(run.getSharedState());
        ArtifactDebugView artifacts = artifactsFromShared(shared, plan);
        BudgetDebugView budget = budgetFrom(plan, parseJson(run.getMetrics()));
        List<MemoryUsageView> memory = memoryUsageViews(runId, null, 80);

        return new RunDebugDetailResponse(
                toSummary(run),
                correlation(run),
                plan,
                budget,
                artifacts,
                timeline,
                errors,
                mcpCalls,
                skills,
                memory,
                truncated,
                nextSeq);
    }

    public Map<String, Object> executionTree(String runId) {
        AgentRun run = agentRunMapper.selectById(runId);
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("runId", runId);
        if (run == null) {
            tree.put("nodes", List.of());
            return tree;
        }
        RunDebugDetailResponse detail = runDetail(runId, 500, null);
        tree.put("run", detail.run());
        tree.put("plan", detail.plan());
        tree.put("timeline", detail.timeline());
        tree.put("errors", detail.errors());
        return tree;
    }

    public McpOpsResponse mcp(boolean probe, String runId, String server, String outcome, int recentLimit) {
        McpInventory inventory = loadMcpInventory(probe);
        Set<String> currentServers = inventory.servers().stream()
                .map(McpInventoryServer::name)
                .filter(StringUtils::hasText)
                .collect(java.util.stream.Collectors.toUnmodifiableSet());
        // The unfiltered global dashboard should describe the current runtime,
        // not mix retired synthetic servers into a live production inventory.
        // An explicit runId/server remains available for historical forensics.
        Set<String> allowedServers = !StringUtils.hasText(runId)
                && !StringUtils.hasText(server) ? currentServers : Set.of();
        boolean filterToCurrentInventory = !StringUtils.hasText(runId)
                && !StringUtils.hasText(server);
        List<McpInvocationView> calls = recentMcpCalls(
                recentLimit, runId, server, outcome, allowedServers,
                filterToCurrentInventory);
        List<McpEndpointStats> endpointStats = mcpEndpointStats(inventory);
        return new McpOpsResponse(
                inventory,
                new McpInvocationPage(calls.size(), calls),
                endpointStats,
                List.of("AVAILABLE", "RATE_LIMITED", "AUTH_REQUIRED", "DOWN", "UNREACHABLE"),
                "状态来自 Python MCP Registry 真实 probe；Endpoint 累计统计与调用明细来自 run_event。");
    }

    public SkillOpsResponse skills(boolean includeDeprecated, int recentLimit) {
        Optional<Map<String, Object>> runtime = runtimeClient.getOpsRuntime(false);
        List<SkillManifestItem> manifest = new ArrayList<>();
        String source = "runtime_unreachable";
        boolean reachable = false;
        Object root = null;
        int count = 0;
        int activeCount = 0;
        int deprecatedCount = 0;
        List<String> advertised = List.of();
        String runtimeError = null;
        if (runtime.isPresent()) {
            reachable = true;
            Object skillsNode = runtime.get().get("skills");
            if (skillsNode instanceof Map<?, ?> skillsMap) {
                @SuppressWarnings("unchecked")
                Map<String, Object> skills = (Map<String, Object>) skillsMap;
                source = String.valueOf(skills.getOrDefault("source", "python_skill_manager"));
                root = skills.get("root");
                count = intOf(skills.get("count"));
                activeCount = intOf(skills.get("activeCount"));
                deprecatedCount = intOf(skills.get("deprecatedCount"));
                if (skills.get("advertisedTools") instanceof List<?> list) {
                    advertised = list.stream().map(String::valueOf).toList();
                }
                if (skills.get("error") != null) {
                    runtimeError = String.valueOf(skills.get("error"));
                }
                manifest = filterSkills(skills.get("skills"), includeDeprecated);
            }
        } else {
            runtimeError = "Python /internal/ops/runtime unavailable";
        }
        Map<String, Object> usage = skillUsageFromEvents(recentLimit);
        @SuppressWarnings("unchecked")
        List<SkillUsageView> events = (List<SkillUsageView>) usage.get("events");
        @SuppressWarnings("unchecked")
        List<SkillAggUsage> bySkill = mergeManifestSkillUsage(
                manifest, (List<SkillAggUsage>) usage.get("bySkill"));
        return new SkillOpsResponse(
                source, reachable, root, count, activeCount, deprecatedCount, advertised,
                manifest, events, bySkill, runtimeError,
                "默认展示 Python runtime ACTIVE skills；选择/应用来自 run_event。");
    }

    static List<SkillAggUsage> mergeManifestSkillUsage(
            List<SkillManifestItem> manifest, List<SkillAggUsage> eventUsage) {
        Map<String, SkillAggUsage> observed = new LinkedHashMap<>();
        for (SkillAggUsage usage : eventUsage == null ? List.<SkillAggUsage>of() : eventUsage) {
            if (usage != null && StringUtils.hasText(usage.skillId())) {
                observed.put(usage.skillId(), usage);
            }
        }
        List<SkillAggUsage> result = new ArrayList<>();
        for (SkillManifestItem skill : manifest == null ? List.<SkillManifestItem>of() : manifest) {
            if (skill == null || !StringUtils.hasText(skill.skillId())) {
                continue;
            }
            SkillAggUsage actual = observed.remove(skill.skillId());
            result.add(actual != null ? actual
                    : new SkillAggUsage(skill.skillId(), 0, 0, 0, 0, 0, 0,
                    null, null, skill.hash(), skill.version()));
        }
        result.addAll(observed.values());
        return result;
    }

    /**
     * Builds a call-centric RAG read model from the immutable run event ledger.
     *
     * <p>{@code tool.started/completed/failed} is the invocation source of
     * truth; optional {@code retrieval.*} events enrich it with stage and
     * ranking telemetry. This means failures and historical calls remain
     * visible even when an older runtime did not emit a retrieval summary.</p>
     */
    public RagOpsResponse rag(int limit, String runId, String agentId, String outcome) {
        int cap = Math.max(1, Math.min(limit, 500));
        List<String> types = new ArrayList<>(TOOL_EVENT_TYPES);
        types.addAll(RETRIEVAL_EVENT_TYPES);
        QueryWrapper<RunEvent> q = new QueryWrapper<RunEvent>()
                .in("event_type", types)
                .in("tool_name", RETRIEVAL_TOOL_NAMES)
                .orderByDesc("create_time")
                .orderByDesc("seq")
                .last("limit " + Math.min(cap * 12, 6000));
        if (StringUtils.hasText(runId)) {
            q.eq("run_id", runId.trim());
        }
        if (StringUtils.hasText(agentId)) {
            q.eq("agent_id", agentId.trim());
        }
        return assembleRag(runEventMapper.selectList(q), cap, outcome);
    }

    RagOpsResponse assembleRag(List<RunEvent> rawEvents, int limit, String outcomeFilter) {
        int cap = Math.max(1, Math.min(limit, 500));
        List<RunEvent> events = rawEvents == null ? new ArrayList<>() : new ArrayList<>(rawEvents);
        events.sort(Comparator
                .comparing(RunEvent::getCreateTime,
                        Comparator.nullsFirst(Comparator.naturalOrder()))
                .thenComparing(RunEvent::getSeq,
                        Comparator.nullsFirst(Comparator.naturalOrder())));

        List<RagDraft> drafts = new ArrayList<>();
        Map<String, RagDraft> byCallId = new HashMap<>();
        for (RunEvent event : events) {
            String eventType = event.getEventType() == null ? "" : event.getEventType();
            if (!TOOL_EVENT_TYPES.contains(eventType)
                    && !RETRIEVAL_EVENT_TYPES.contains(eventType)) {
                continue;
            }
            Map<String, Object> payload = asMap(parseJson(event.getPayload()));
            String payloadTool = firstText(payload, "toolName", "tool", "retrievalTool");
            String toolName = StringUtils.hasText(payloadTool) ? payloadTool : event.getToolName();
            if (!RETRIEVAL_TOOL_NAMES.contains(toolName)) {
                continue;
            }
            String toolCallId = firstText(payload, "toolCallId", "callId", "invocationId");
            RagDraft draft = null;
            if (StringUtils.hasText(toolCallId)) {
                draft = byCallId.get(callKey(event.getRunId(), toolCallId));
            }
            if (draft == null && !"tool.started".equals(eventType)
                    && !"retrieval.started".equals(eventType)) {
                boolean wantsTelemetry = eventType.startsWith("retrieval.");
                draft = findCompatibleDraft(drafts, event, toolName, wantsTelemetry, payload);
            }
            if (draft == null) {
                draft = new RagDraft(event, toolName, toolCallId);
                drafts.add(draft);
            }
            if (StringUtils.hasText(toolCallId)) {
                draft.toolCallId = toolCallId;
                byCallId.put(callKey(event.getRunId(), toolCallId), draft);
            }

            if ("tool.started".equals(eventType) || "retrieval.started".equals(eventType)) {
                draft.mergeStarted(event, payload);
            } else if ("tool.completed".equals(eventType) || "tool.failed".equals(eventType)) {
                draft.mergeCompleted(event, payload, "tool.failed".equals(eventType));
            } else {
                draft.mergeRetrieval(event, payload, "retrieval.failed".equals(eventType));
            }
        }

        List<RagRetrievalView> items = drafts.stream()
                .map(RagDraft::toView)
                .filter(item -> !StringUtils.hasText(outcomeFilter)
                        || outcomeFilter.trim().equalsIgnoreCase(item.outcome()))
                .sorted(Comparator
                        .comparing(OpsDebugService::ragSortTime,
                                Comparator.nullsLast(Comparator.reverseOrder()))
                        .thenComparing(RagRetrievalView::seq,
                                Comparator.nullsLast(Comparator.reverseOrder())))
                .limit(cap)
                .toList();
        return buildRagResponse(items);
    }

    public MemoryOpsResponse memory(int limit, String scope, String source, String runId,
                                    String decision, boolean includeBenchmark,
                                    boolean includeControlFailure) {
        int cap = Math.max(1, Math.min(limit, 200));
        QueryWrapper<MemoryEntryRow> q = new QueryWrapper<MemoryEntryRow>()
                .orderByDesc("update_time");
        if (StringUtils.hasText(scope)) {
            q.eq("owner_scope", scope.trim().toUpperCase(Locale.ROOT));
        }
        if (StringUtils.hasText(source)) {
            q.eq("source", source.trim());
        }
        if (StringUtils.hasText(runId)) {
            q.eq("run_id", runId.trim());
        }
        q.last("limit " + Math.min(cap * 3, 600));
        List<MemoryEntryRow> raw = memoryEntryMapper.selectList(q);

        List<Map<String, Object>> entries = new ArrayList<>();
        Map<String, Long> byType = new LinkedHashMap<>();
        Map<String, Long> byScope = new LinkedHashMap<>();
        Map<String, Long> bySource = new LinkedHashMap<>();
        int skipped = 0;
        for (MemoryEntryRow row : raw) {
            if (!includeBenchmark && isBenchmarkMemory(row)) {
                skipped++;
                continue;
            }
            if (!includeControlFailure && isControlFailureMemory(row)) {
                skipped++;
                continue;
            }
            Map<String, Object> item = memoryItem(row);
            entries.add(item);
            byType.merge(row.getType() == null ? "UNKNOWN" : row.getType(), 1L, Long::sum);
            byScope.merge(row.getOwnerScope() == null ? "UNKNOWN" : row.getOwnerScope(), 1L, Long::sum);
            bySource.merge(row.getSource() == null ? "UNKNOWN" : row.getSource(), 1L, Long::sum);
            if (entries.size() >= cap) {
                break;
            }
        }
        List<MemoryUsageView> usage = memoryUsageViews(
                StringUtils.hasText(runId) ? runId : null, decision, cap);
        Map<String, Object> defaults = new LinkedHashMap<>();
        defaults.put("hideBenchmark", !includeBenchmark);
        defaults.put("hideControlFailure", !includeControlFailure);
        defaults.put("ttl", Map.of(
                "mode", "ABSOLUTE",
                "renewOnUse", false,
                "expiringSoonDays", 7,
                "typeDefaultDays", MemoryService.ttlPolicyDays()));
        return new MemoryOpsResponse(
                entries.size(), skipped, byType, byScope, bySource, entries, usage,
                defaults,
                agentMemoryService.overview());
    }

    public RunDebugSummary toSummary(AgentRun run) {
        Long queueWaitMs = null;
        Long runtimeMs = null;
        Long durationMs = null;
        if (run.getCreatedAt() != null && run.getStartedAt() != null) {
            queueWaitMs = Duration.between(run.getCreatedAt(), run.getStartedAt()).toMillis();
        }
        if (run.getStartedAt() != null && run.getFinishedAt() != null) {
            runtimeMs = Duration.between(run.getStartedAt(), run.getFinishedAt()).toMillis();
        }
        if (run.getCreatedAt() != null && run.getFinishedAt() != null) {
            durationMs = Duration.between(run.getCreatedAt(), run.getFinishedAt()).toMillis();
        }
        return new RunDebugSummary(
                run.getRunId(), run.getConversationId(), run.getUserId(), run.getTraceId(),
                run.getSourceTaskTraceId(), run.getRevisionNo(), run.getRunType(), run.getStatus(),
                run.getCurrentAgent(), run.getCurrentTool(), run.getCurrentPhase(),
                run.getErrorCode(), truncate(run.getErrorMessage(), 500),
                parseJson(run.getSkillVersions()), parseJson(run.getPromptVersions()),
                parseJson(run.getMetrics()),
                run.getCreatedAt(), run.getStartedAt(), run.getFinishedAt(), run.getUpdatedAt(),
                queueWaitMs, runtimeMs, durationMs);
    }

    public Map<String, Object> runSummaryMap(AgentRun run) {
        RunDebugSummary s = toSummary(run);
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("runId", s.runId());
        item.put("conversationId", s.conversationId());
        item.put("userId", s.userId());
        item.put("traceId", s.traceId());
        item.put("sourceTaskTraceId", s.sourceTaskTraceId());
        item.put("revisionNo", s.revisionNo());
        item.put("runType", s.runType());
        item.put("status", s.status());
        item.put("currentAgent", s.currentAgent());
        item.put("currentTool", s.currentTool());
        item.put("currentPhase", s.currentPhase());
        item.put("errorCode", s.errorCode());
        item.put("errorMessage", truncate(s.errorMessage(), 200));
        item.put("skillVersions", s.skillVersions());
        item.put("createdAt", s.createdAt());
        item.put("startedAt", s.startedAt());
        item.put("finishedAt", s.finishedAt());
        item.put("updatedAt", s.updatedAt());
        item.put("queueWaitMs", s.queueWaitMs());
        item.put("runtimeMs", s.runtimeMs());
        item.put("durationMs", s.durationMs());
        return item;
    }

    public Map<String, Object> eventItem(RunEvent event) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("runId", event.getRunId());
        item.put("conversationId", event.getConversationId());
        item.put("traceId", event.getTraceId());
        item.put("seq", event.getSeq());
        item.put("eventType", event.getEventType());
        item.put("agentId", event.getAgentId());
        item.put("toolName", event.getToolName());
        item.put("payload", parseJson(event.getPayload()));
        item.put("createTime", event.getCreateTime());
        item.put("outcome", deriveOutcome(event.getEventType()).name());
        return item;
    }

    public Object redact(Object value) {
        if (value == null) {
            return null;
        }
        String text = String.valueOf(value);
        if (text.length() > 240) {
            text = text.substring(0, 240) + "…";
        }
        return text.replaceAll("(?i)(token|secret|password|authorization)=\\S+", "$1=***");
    }

    public Object parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return Map.of();
        }
        try {
            JsonNode node = objectMapper.readTree(raw);
            if (node.isObject() || node.isArray()) {
                return objectMapper.convertValue(node, Object.class);
            }
            return raw;
        } catch (Exception e) {
            return raw;
        }
    }

    public boolean isMcpPayload(Object payload) {
        if (!(payload instanceof Map<?, ?> map)) {
            return false;
        }
        Object kind = map.get("kind");
        if (kind != null && "mcp".equalsIgnoreCase(String.valueOf(kind))) {
            return true;
        }
        return map.get("mcpServer") != null;
    }

    // ------------------------------------------------------------------

    private CorrelationView correlation(AgentRun run) {
        List<Map<String, Object>> siblings = new ArrayList<>();
        List<Map<String, Object>> retries = new ArrayList<>();
        if (StringUtils.hasText(run.getConversationId())) {
            List<AgentRun> related = agentRunMapper.selectList(new QueryWrapper<AgentRun>()
                    .eq("conversation_id", run.getConversationId())
                    .orderByAsc("created_at")
                    .last("limit 40"));
            for (AgentRun peer : related) {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("runId", peer.getRunId());
                row.put("revisionNo", peer.getRevisionNo());
                row.put("status", peer.getStatus());
                row.put("errorCode", peer.getErrorCode());
                row.put("createdAt", peer.getCreatedAt());
                siblings.add(row);
                if (peer.getRetryCount() != null && peer.getRetryCount() > 0
                        || (peer.getErrorCode() != null && peer.getRunId().equals(run.getRunId()))) {
                    retries.add(row);
                }
            }
        }
        return new CorrelationView(
                run.getRunId(), run.getConversationId(), run.getTraceId(),
                run.getSourceTaskTraceId(), run.getRevisionNo(),
                siblings, retries);
    }

    @SuppressWarnings("unchecked")
    private PlanDebugView planFromPayload(Object payload) {
        if (!(payload instanceof Map<?, ?> map)) {
            return PlanDebugView.empty();
        }
        List<String> plan = castStringList(map.get("plan"));
        List<List<String>> groups = castGroups(map.get("parallelGroups"));
        Map<String, Object> selected = map.get("selectedBecause") instanceof Map<?, ?> m
                ? castObjectMap(m) : Map.of();
        Map<String, Object> skipped = map.get("skippedBecause") instanceof Map<?, ?> m
                ? castObjectMap(m) : Map.of();
        List<Object> edges = map.get("artifactEdges") instanceof List<?> list
                ? new ArrayList<>(list) : List.of();
        List<String> goals = castStringList(map.get("goalArtifacts"));
        Object budget = map.get("budgetPlan") != null ? map.get("budgetPlan") : map.get("budget");
        return new PlanDebugView(
                plan, groups,
                str(map.get("reason")),
                str(map.get("requiredTerminalAgent")),
                selected, skipped, edges, goals, budget != null ? budget : Map.of(),
                true);
    }

    private ArtifactDebugView artifactsFromShared(Object shared, PlanDebugView plan) {
        Map<String, Object> artifacts = Map.of();
        if (shared instanceof Map<?, ?> root && root.get("artifacts") instanceof Map<?, ?> arts) {
            artifacts = castObjectMap(arts);
        }
        List<String> present = new ArrayList<>();
        for (String key : BUSINESS_ARTIFACT_KEYS) {
            Object value = artifacts.get(key);
            if (value == null) {
                continue;
            }
            if (value instanceof Map<?, ?> m && !m.isEmpty()) {
                present.add(key);
            } else if (value instanceof List<?> list && !list.isEmpty()) {
                present.add(key);
            } else if (value instanceof String s && StringUtils.hasText(s)) {
                present.add(key);
            }
        }
        List<String> required = plan.goalArtifacts() != null ? plan.goalArtifacts() : List.of();
        List<String> missing = required.stream().filter(k -> !present.contains(k)).toList();
        return new ArtifactDebugView(artifacts, present, required, missing,
                plan.artifactEdges() != null ? plan.artifactEdges() : List.of());
    }

    private BudgetDebugView budgetFrom(PlanDebugView plan, Object metrics) {
        Integer llm = null;
        Integer tools = null;
        Integer prompt = null;
        Integer completion = null;
        Object cost = null;
        if (metrics instanceof Map<?, ?> m) {
            llm = intOrNull(m.get("llmCalls") != null ? m.get("llmCalls") : m.get("llmCallCount"));
            tools = intOrNull(m.get("toolCalls") != null ? m.get("toolCalls") : m.get("toolCallCount"));
            prompt = intOrNull(m.get("promptTokens"));
            completion = intOrNull(m.get("completionTokens"));
            cost = m.get("cost") != null ? m.get("cost") : m.get("estimatedCostCny");
        }
        return new BudgetDebugView(plan.budgetPlan(), metrics, llm, tools, prompt, completion, cost);
    }

    private List<MemoryUsageView> memoryUsageViews(String runId, String decision, int limit) {
        List<RunMemoryUsageRow> rows = StringUtils.hasText(runId)
                ? memoryUsageService.listForRun(runId, decision, limit)
                : memoryUsageService.listRecent(null, decision, limit);
        Map<String, MemoryEntryRow> byId = new LinkedHashMap<>();
        Set<String> memoryIds = new LinkedHashSet<>();
        for (RunMemoryUsageRow row : rows) {
            if (row.getMemoryId() != null) {
                memoryIds.add(row.getMemoryId());
            }
        }
        if (!memoryIds.isEmpty()) {
            for (MemoryEntryRow entry : memoryEntryMapper.selectBatchIds(memoryIds)) {
                byId.put(entry.getMemoryId(), entry);
            }
        }
        List<MemoryUsageView> out = new ArrayList<>();
        LocalDateTime now = LocalDateTime.now();
        for (RunMemoryUsageRow row : rows) {
            MemoryEntryRow entry = byId.get(row.getMemoryId());
            Long ageAtUseSeconds = memoryAgeAtUseSeconds(entry, row.getCreateTime());
            out.add(new MemoryUsageView(
                    row.getId(), row.getRunId(), row.getMemoryId(), row.getConsumerAgent(),
                    row.getConsumerVersion(), entry != null ? entry.getProducerVersion() : null,
                    row.getRankNo(),
                    row.getVectorScore() != null ? row.getVectorScore().doubleValue() : null,
                    row.getLexicalScore() != null ? row.getLexicalScore().doubleValue() : null,
                    row.getRecencyScore() != null ? row.getRecencyScore().doubleValue() : null,
                    row.getFinalScore() != null ? row.getFinalScore().doubleValue() : null,
                    row.getDecision(), row.getIgnoredReason(),
                    utcTimestamp(row.getCreateTime()),
                    row.getCreateTime(),
                    entry != null ? entry.getType() : null,
                    entry != null ? entry.getOwnerScope() : null,
                    entry != null ? entry.getSource() : null,
                    entry != null ? truncate(entry.getContent(), 200) : null,
                    entry != null ? localTimestamp(entry.getCreateTime()) : null,
                    entry != null ? localTimestamp(entry.getUpdateTime()) : null,
                    ageAtUseSeconds,
                    entry != null ? memoryTtlView(entry, now) : null));
        }
        return out;
    }

    /**
     * memory_entry timestamps are JVM-local, while run_memory_usage deliberately
     * persists the workflow occurredAt instant as a UTC LocalDateTime. Compare
     * Instants so the eight-hour storage convention difference is not mistaken
     * for a negative memory age.
     */
    static Long memoryAgeAtUseSeconds(MemoryEntryRow entry, LocalDateTime usageUtc) {
        if (entry == null || entry.getCreateTime() == null || usageUtc == null) {
            return null;
        }
        Instant created = entry.getCreateTime().atZone(ZoneId.systemDefault()).toInstant();
        Instant used = usageUtc.toInstant(ZoneOffset.UTC);
        return Duration.between(created, used).getSeconds();
    }

    private McpInventory loadMcpInventory(boolean probe) {
        Optional<Map<String, Object>> runtime = runtimeClient.getOpsRuntime(probe);
        if (runtime.isEmpty()) {
            return new McpInventory("runtime_unreachable", false, false, null, List.of(), 0, null,
                    List.of(), "Python /internal/ops/runtime unavailable");
        }
        Object mcpNode = runtime.get().get("mcp");
        if (!(mcpNode instanceof Map<?, ?> mcpMap)) {
            return new McpInventory("python_mcp_registry", true, false, null, List.of(), 0, null,
                    List.of(), "Python runtime response is missing the MCP registry snapshot");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> mcp = (Map<String, Object>) mcpMap;
        List<McpInventoryServer> servers = flattenMcpServers(mcp.get("servers"));
        return new McpInventory(
                String.valueOf(mcp.getOrDefault("source", "python_mcp_registry")),
                true,
                Boolean.TRUE.equals(mcp.get("probed")),
                mcp.get("lastProbeAt"),
                mcp.getOrDefault("availableTools", List.of()),
                mcp.getOrDefault("toolCount", 0),
                mcp.get("configPath"),
                servers,
                mcp.get("error") != null ? String.valueOf(mcp.get("error")) : null);
    }

    private List<McpInventoryServer> flattenMcpServers(Object serversNode) {
        List<McpInventoryServer> out = new ArrayList<>();
        if (!(serversNode instanceof Map<?, ?> map)) {
            return out;
        }
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (!(entry.getValue() instanceof Map<?, ?> raw)) {
                continue;
            }
            List<String> tools = raw.get("tools") instanceof List<?> list
                    ? list.stream().map(String::valueOf).toList() : List.of();
            out.add(new McpInventoryServer(
                    String.valueOf(entry.getKey()),
                    str(raw.get("status") != null ? raw.get("status") : "UNREACHABLE"),
                    str(raw.get("transport")),
                    str(raw.get("description")),
                    longOrNull(raw.get("latencyMs")),
                    raw.get("circuitOpen") instanceof Boolean b ? b : null,
                    raw.get("optional") instanceof Boolean b ? b : null,
                    tools,
                    str(raw.get("error"))));
        }
        out.sort((a, b) -> a.name().compareTo(b.name()));
        return out;
    }

    private List<McpInvocationView> recentMcpCalls(int limit, String runId, String server,
                                                   String outcome,
                                                   Set<String> allowedServers,
                                                   boolean filterToCurrentInventory) {
        int cap = Math.max(1, Math.min(limit, 200));
        QueryWrapper<RunEvent> q = new QueryWrapper<RunEvent>()
                .in("event_type", MCP_EVENT_TYPES)
                .orderByDesc("create_time")
                .last("limit " + Math.min(cap * 5, 500));
        if (StringUtils.hasText(runId)) {
            q.eq("run_id", runId.trim());
        }
        List<RunEvent> events = runEventMapper.selectList(q);
        Map<String, McpInvocationView> merged = new LinkedHashMap<>();
        for (RunEvent event : events) {
            Object payload = parseJson(event.getPayload());
            if (!isMcpPayload(payload)) {
                continue;
            }
            if (payload instanceof Map<?, ?> raw
                    && "CATALOG_EXPOSED".equalsIgnoreCase(str(raw.get("lifecycleStage")))) {
                // Catalog exposure is model input, not an invocation.
                continue;
            }
            McpInvocationView view = toMcpInvocation(event, payload);
            if (filterToCurrentInventory
                    && (view.server() == null || !allowedServers.contains(view.server()))) {
                continue;
            }
            if (StringUtils.hasText(server) && (view.server() == null
                    || !server.equalsIgnoreCase(view.server()))) {
                continue;
            }
            String key = String.valueOf(view.runId()) + "|"
                    + (StringUtils.hasText(view.toolCallId())
                    ? view.toolCallId()
                    : String.valueOf(view.server()) + "|" + view.tool() + "|" + view.seq());
            merged.merge(key, view, OpsDebugService::mergeMcpInvocation);
        }
        List<McpInvocationView> out = new ArrayList<>();
        for (McpInvocationView view : merged.values()) {
            if (StringUtils.hasText(outcome) && (view.outcome() == null
                    || !outcome.equalsIgnoreCase(view.outcome()))) {
                continue;
            }
            out.add(view);
            if (out.size() >= cap) break;
        }
        return out;
    }

    private List<McpEndpointStats> mcpEndpointStats(McpInventory inventory) {
        Map<String, String> serverByTool = new LinkedHashMap<>();
        for (McpInventoryServer server : inventory.servers()) {
            for (String tool : server.tools() == null ? List.<String>of() : server.tools()) {
                if (StringUtils.hasText(tool)) {
                    serverByTool.put(tool, server.name());
                }
            }
        }
        if (serverByTool.isEmpty()) {
            return List.of();
        }

        List<RunEvent> events = runEventMapper.selectList(new QueryWrapper<RunEvent>()
                .in("event_type", MCP_EVENT_TYPES)
                .in("tool_name", serverByTool.keySet())
                .orderByDesc("create_time")
                .orderByDesc("seq"));
        Map<String, McpInvocationView> merged = new LinkedHashMap<>();
        for (RunEvent event : events) {
            Object payload = parseJson(event.getPayload());
            if (!isMcpPayload(payload)) {
                continue;
            }
            if (payload instanceof Map<?, ?> raw
                    && "CATALOG_EXPOSED".equalsIgnoreCase(str(raw.get("lifecycleStage")))) {
                continue;
            }
            McpInvocationView view = toMcpInvocation(event, payload);
            if (!serverByTool.containsKey(view.tool())) {
                continue;
            }
            String key = String.valueOf(view.runId()) + "|"
                    + (StringUtils.hasText(view.toolCallId())
                    ? view.toolCallId()
                    : view.tool() + "|" + view.seq());
            merged.merge(key, view, OpsDebugService::mergeMcpInvocation);
        }

        Map<String, List<McpInvocationView>> callsByTool = new LinkedHashMap<>();
        serverByTool.keySet().forEach(tool -> callsByTool.put(tool, new ArrayList<>()));
        for (McpInvocationView call : merged.values()) {
            callsByTool.computeIfAbsent(call.tool(), ignored -> new ArrayList<>()).add(call);
        }

        List<McpEndpointStats> result = new ArrayList<>();
        for (Map.Entry<String, String> endpoint : serverByTool.entrySet()) {
            String tool = endpoint.getKey();
            List<McpInvocationView> calls = callsByTool.getOrDefault(tool, List.of());
            long success = calls.stream().filter(call -> "SUCCESS".equalsIgnoreCase(call.outcome())).count();
            long rejected = calls.stream().filter(call -> "REJECTED".equalsIgnoreCase(call.outcome())).count();
            long failed = calls.stream().filter(call -> "FAILED".equalsIgnoreCase(call.outcome())).count();
            long running = calls.stream().filter(call -> !terminalMcpOutcome(call.outcome())).count();
            long terminal = success + failed + rejected;
            Double successRate = terminal > 0
                    ? Math.round(1000D * success / terminal) / 10D : null;
            List<Long> durations = calls.stream()
                    .map(McpInvocationView::durationMs)
                    .filter(value -> value != null && value >= 0)
                    .sorted()
                    .toList();
            Long average = durations.isEmpty() ? null
                    : Math.round(durations.stream().mapToLong(Long::longValue).average().orElse(0));
            McpInvocationView latest = calls.stream()
                    .max(Comparator.comparing(OpsDebugService::mcpCallTimestamp,
                            Comparator.nullsFirst(Comparator.naturalOrder())))
                    .orElse(null);
            result.add(new McpEndpointStats(
                    endpoint.getValue(), tool, calls.size(), success, failed, rejected, running,
                    successRate, average, percentileLong(durations, 0.50),
                    percentileLong(durations, 0.90),
                    durations.isEmpty() ? null : durations.get(durations.size() - 1),
                    latest != null ? latest.runId() : null,
                    latest != null ? mcpCallTimestamp(latest) : null));
        }
        result.sort(Comparator.comparing(McpEndpointStats::server)
                .thenComparing(McpEndpointStats::endpoint));
        return result;
    }

    private static String mcpCallTimestamp(McpInvocationView call) {
        if (call == null) return null;
        return firstNonBlank(
                firstNonBlank(call.endedAt(), call.occurredAt(), call.startedAt()),
                localTimestamp(call.createTime()));
    }

    private static Long percentileLong(List<Long> sorted, double percentile) {
        if (sorted == null || sorted.isEmpty()) return null;
        int index = Math.max(0, Math.min(sorted.size() - 1,
                (int) Math.ceil(percentile * sorted.size()) - 1));
        return sorted.get(index);
    }

    private static McpInvocationView mergeMcpInvocation(McpInvocationView first,
                                                         McpInvocationView second) {
        String startedAt = earliestTimestamp(first.startedAt(), second.startedAt());
        String endedAt = latestTimestamp(first.endedAt(), second.endedAt());
        Long durationMs = first.durationMs() != null ? first.durationMs() : second.durationMs();
        if (durationMs == null && startedAt != null && endedAt != null) {
            durationMs = elapsedMs(startedAt, endedAt);
        }
        String outcome = mergeMcpOutcome(first.outcome(), second.outcome());
        String stage = terminalMcpOutcome(outcome)
                ? ("FAILED".equals(outcome) ? "FAILED" : "COMPLETED")
                : firstNonBlank(first.lifecycleStage(), second.lifecycleStage());
        return new McpInvocationView(
                firstNonBlank(first.runId(), second.runId()),
                firstNonBlank(first.traceId(), second.traceId()),
                minSeq(first.seq(), second.seq()),
                firstNonBlank(first.toolCallId(), second.toolCallId()),
                firstNonBlank(first.server(), second.server()),
                firstNonBlank(first.tool(), second.tool()),
                firstNonBlank(first.agent(), second.agent()),
                stage,
                outcome,
                durationMs,
                maxInt(first.retryCount(), second.retryCount()),
                first.cacheHit() != null ? first.cacheHit() : second.cacheHit(),
                first.arguments() != null ? first.arguments() : second.arguments(),
                first.resultPreview() != null ? first.resultPreview() : second.resultPreview(),
                firstNonBlank(first.error(), second.error()),
                earliestTimestamp(first.occurredAt(), second.occurredAt()),
                startedAt,
                endedAt,
                first.createTime() != null ? first.createTime() : second.createTime());
    }

    private static String mergeMcpOutcome(String first, String second) {
        if ("FAILED".equalsIgnoreCase(first) || "REJECTED".equalsIgnoreCase(first)) {
            return first.toUpperCase(Locale.ROOT);
        }
        if ("FAILED".equalsIgnoreCase(second) || "REJECTED".equalsIgnoreCase(second)) {
            return second.toUpperCase(Locale.ROOT);
        }
        if (terminalMcpOutcome(first)) return first.toUpperCase(Locale.ROOT);
        if (terminalMcpOutcome(second)) return second.toUpperCase(Locale.ROOT);
        return firstNonBlank(first, second, "RUNNING").toUpperCase(Locale.ROOT);
    }

    private static boolean terminalMcpOutcome(String outcome) {
        return "SUCCESS".equalsIgnoreCase(outcome)
                || "FAILED".equalsIgnoreCase(outcome)
                || "REJECTED".equalsIgnoreCase(outcome);
    }

    private static String earliestTimestamp(String first, String second) {
        if (!StringUtils.hasText(first)) return second;
        if (!StringUtils.hasText(second)) return first;
        return first.compareTo(second) <= 0 ? first : second;
    }

    private static String latestTimestamp(String first, String second) {
        if (!StringUtils.hasText(first)) return second;
        if (!StringUtils.hasText(second)) return first;
        return first.compareTo(second) >= 0 ? first : second;
    }

    private static Long elapsedMs(String startedAt, String endedAt) {
        try {
            return Math.max(0L, Duration.between(Instant.parse(startedAt), Instant.parse(endedAt)).toMillis());
        } catch (RuntimeException ignored) {
            return null;
        }
    }

    private static Integer maxInt(Integer first, Integer second) {
        if (first == null) return second;
        if (second == null) return first;
        return Math.max(first, second);
    }

    private McpInvocationView toMcpInvocation(RunEvent event, Object payload) {
        Map<?, ?> p = payload instanceof Map<?, ?> map ? map : Map.of();
        String type = event.getEventType() == null ? "" : event.getEventType();
        String outcome;
        Object payloadOutcome = p.get("outcome");
        if (type.endsWith("failed")) {
            outcome = "FAILED";
        } else if (type.endsWith("completed")) {
            Object rp = p.get("resultPreview");
            String rpStr = rp instanceof String s ? s : (rp != null ? rp.toString() : "");
            String payloadStatus = payloadOutcome instanceof String po ? po : "";
            outcome = "FAILED".equalsIgnoreCase(payloadStatus)
                    || "REJECTED".equalsIgnoreCase(payloadStatus)
                    || rpStr.contains("\"success\":false")
                    || rpStr.contains("\"success\": false")
                    ? "FAILED" : "SUCCESS";
        } else if (payloadOutcome instanceof String po && !po.isBlank()) {
            outcome = po;
        } else {
            outcome = "RUNNING";
        }
        String fallbackTime = eventTime(event);
        String toolName = firstNonBlank(event.getToolName(),
                firstTextDeep(p, "toolName", "tool", "name"));
        String occurredAt = firstNonBlank(
                firstTextDeep(p, "occurredAt", "timestamp"),
                fallbackTime);
        String startedAt = firstTextDeep(p, "startedAt", "startTime");
        String endedAt = firstTextDeep(p, "endedAt", "completedAt", "endTime");
        if (!StringUtils.hasText(startedAt) && type.endsWith("started")) {
            startedAt = occurredAt;
        }
        if (!StringUtils.hasText(endedAt)
                && (type.endsWith("completed") || type.endsWith("failed"))) {
            endedAt = occurredAt;
        }
        return new McpInvocationView(
                event.getRunId(), event.getTraceId(), event.getSeq(),
                str(p.get("toolCallId")),
                str(p.get("mcpServer")),
                toolName,
                event.getAgentId(),
                str(p.get("lifecycleStage")),
                outcome,
                longOrNull(p.get("durationMs")),
                intOrNull(p.get("retryCount")),
                p.get("cacheHit") instanceof Boolean b ? b : null,
                redact(p.get("arguments")),
                redact(p.get("resultPreview")),
                str(redact(p.get("error"))),
                occurredAt,
                startedAt,
                endedAt,
                event.getCreateTime());
    }

    private SkillUsageView toSkillUsage(RunEvent event, Object payload) {
        Map<?, ?> p = payload instanceof Map<?, ?> map ? map : Map.of();
        String skillId = event.getToolName();
        if (p.get("skillId") != null) {
            skillId = String.valueOf(p.get("skillId"));
        }
        String runHash = str(p.get("skillHash"));
        String manifestHash = str(p.get("manifestHash"));
        Boolean drift = null;
        if (StringUtils.hasText(runHash) && StringUtils.hasText(manifestHash)) {
            drift = !runHash.equals(manifestHash);
        } else if (p.get("hashDrift") instanceof Boolean b) {
            drift = b;
        }
        List<String> required = p.get("requiredTools") instanceof List<?> list
                ? list.stream().map(String::valueOf).toList()
                : (p.get("requiredMcp") instanceof List<?> list2
                ? list2.stream().map(String::valueOf).toList() : List.of());
        String fallbackTime = eventTime(event);
        String occurredAt = firstNonBlank(
                firstTextDeep(p, "occurredAt", "timestamp"),
                fallbackTime);
        String startedAt = firstTextDeep(p, "startedAt", "startTime");
        String endedAt = firstTextDeep(p, "endedAt", "completedAt", "endTime");
        String type = event.getEventType() == null ? "" : event.getEventType();
        if (!StringUtils.hasText(startedAt) && type.endsWith("started")) {
            startedAt = occurredAt;
        }
        if (!StringUtils.hasText(endedAt)
                && (type.endsWith("completed") || type.endsWith("applied")
                || type.endsWith("failed"))) {
            endedAt = occurredAt;
        }
        return new SkillUsageView(
                event.getRunId(), skillId, event.getAgentId(), event.getEventType(),
                str(p.get("lifecycleStage")),
                str(p.get("triggerReason") != null
                        ? p.get("triggerReason") : p.get("reason")),
                str(p.get("skillVersion")),
                runHash, runHash, manifestHash, drift, required, payload,
                occurredAt, startedAt, endedAt, event.getCreateTime());
    }

    private ErrorDiagnosticView toError(RunEvent event, Object payload, boolean rootCause) {
        Map<?, ?> p = payload instanceof Map<?, ?> map ? map : Map.of();
        String code = str(p.get("errorCode"));
        String message = str(p.get("errorMessage") != null ? p.get("errorMessage")
                : (p.get("error") != null ? p.get("error") : p.get("detail")));
        return new ErrorDiagnosticView(
                event.getSeq(), event.getEventType(), event.getAgentId(), event.getToolName(),
                code, truncate(message, 800), payload, event.getCreateTime(), rootCause);
    }

    private Map<String, Object> skillUsageFromEvents(int limit) {
        int cap = Math.max(1, Math.min(limit, 500));
        List<RunEvent> events = runEventMapper.selectList(new QueryWrapper<RunEvent>()
                .in("event_type", SKILL_EVENT_TYPES)
                .orderByDesc("create_time")
                .last("limit " + cap));
        List<SkillUsageView> items = new ArrayList<>();
        Map<String, SkillAggUsage> bySkill = new LinkedHashMap<>();
        for (RunEvent event : events) {
            Object payload = parseJson(event.getPayload());
            SkillUsageView view = toSkillUsage(event, payload);
            items.add(view);
            String skillId = view.skillId();
            if (!StringUtils.hasText(skillId)) {
                continue;
            }
            SkillAggUsage agg = bySkill.get(skillId);
            long catalog = agg != null ? agg.catalog() : 0L;
            long selected = agg != null ? agg.selected() : 0L;
            long loaded = agg != null ? agg.loaded() : 0L;
            long applied = agg != null ? agg.applied() : 0L;
            long skipped = agg != null ? agg.skipped() : 0L;
            long failed = agg != null ? agg.failed() : 0L;
            String type = event.getEventType() == null ? "" : event.getEventType();
            if ("skill.catalog".equals(type) || "skill.catalog.exposed".equals(type)) {
                catalog++;
            } else if ("skill.selected".equals(type) || "skill.started".equals(type)) {
                selected++;
            } else if ("skill.loaded".equals(type)) {
                loaded++;
            } else if ("skill.applied".equals(type) || "skill.completed".equals(type)) {
                applied++;
            } else if ("skill.skipped".equals(type)) {
                skipped++;
            } else if ("skill.failed".equals(type)) {
                failed++;
            }
            String lastRunId = agg != null ? agg.lastRunId() : event.getRunId();
            LocalDateTime lastAt = agg != null ? agg.lastAt() : event.getCreateTime();
            String lastHash = agg != null ? agg.lastHash() : view.skillHash();
            String lastVersion = agg != null ? agg.lastVersion() : view.skillVersion();
            bySkill.put(skillId, new SkillAggUsage(
                    skillId, catalog, selected, loaded, applied, skipped, failed,
                    lastRunId, lastAt, lastHash, lastVersion));
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("events", items);
        body.put("bySkill", new ArrayList<>(bySkill.values()));
        return body;
    }

    private List<SkillManifestItem> filterSkills(Object skillsNode, boolean includeDeprecated) {
        if (!(skillsNode instanceof List<?> list)) {
            return List.of();
        }
        List<SkillManifestItem> out = new ArrayList<>();
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> map)) {
                continue;
            }
            boolean deprecated = Boolean.TRUE.equals(map.get("deprecated"));
            boolean adminOnly = Boolean.TRUE.equals(map.get("adminOnly"));
            if (!includeDeprecated && (deprecated || adminOnly)) {
                continue;
            }
            List<String> required = map.get("requiredTools") instanceof List<?> l
                    ? l.stream().map(String::valueOf).toList() : List.of();
            List<String> allowed = map.get("allowedTools") instanceof List<?> l
                    ? l.stream().map(String::valueOf).toList() : List.of();
            out.add(new SkillManifestItem(
                    str(map.get("skillId") != null ? map.get("skillId") : map.get("name")),
                    str(map.get("name")),
                    str(map.get("version")),
                    str(map.get("hash")),
                    str(map.get("status")),
                    str(map.get("description")),
                    deprecated, adminOnly, required, allowed));
        }
        return out;
    }

    private Map<String, Object> memoryItem(MemoryEntryRow row) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("memoryId", row.getMemoryId());
        item.put("type", row.getType());
        item.put("ownerScope", row.getOwnerScope());
        item.put("userId", row.getUserId());
        item.put("conversationId", row.getConversationId());
        item.put("runId", row.getRunId());
        item.put("content", truncate(row.getContent(), 320));
        item.put("source", row.getSource());
        item.put("sourceId", row.getSourceId());
        item.put("confidence", row.getConfidence());
        item.put("status", row.getStatus());
        item.put("version", row.getVersion());
        item.put("producerVersion", row.getProducerVersion());
        item.put("updateTime", row.getUpdateTime());
        item.put("createTime", row.getCreateTime());
        item.put("occurredAt", localTimestamp(row.getCreateTime()));
        item.put("ttl", memoryTtlView(row, LocalDateTime.now()));
        return item;
    }

    static MemoryTtlView memoryTtlView(MemoryEntryRow row, LocalDateTime now) {
        LocalDateTime expiresAt = row.getExpiresAt();
        long defaultDays = MemoryService.defaultTtlDays(row.getType());
        if (expiresAt == null) {
            return new MemoryTtlView(
                    "ABSOLUTE", "NO_EXPIRY", null, null, null, null,
                    defaultDays, false, false);
        }

        Long effectiveSeconds = row.getCreateTime() != null
                ? Math.max(0L, Duration.between(row.getCreateTime(), expiresAt).getSeconds())
                : null;
        long remainingSeconds = Duration.between(now, expiresAt).getSeconds();
        Double remainingPercent = null;
        if (effectiveSeconds != null && effectiveSeconds > 0) {
            double raw = 100.0 * remainingSeconds / effectiveSeconds;
            remainingPercent = Math.round(Math.max(0.0, Math.min(100.0, raw)) * 10.0) / 10.0;
        }
        long defaultSeconds = Duration.ofDays(defaultDays).getSeconds();
        boolean overrideDetected = effectiveSeconds != null
                && Math.abs(effectiveSeconds - defaultSeconds) >= 60;
        String storedStatus = String.valueOf(row.getStatus()).toUpperCase(Locale.ROOT);
        String state;
        if ("EXPIRED".equals(storedStatus) || remainingSeconds <= 0) {
            state = "EXPIRED";
        } else if (!"ACTIVE".equals(storedStatus)) {
            state = storedStatus;
        } else if (remainingSeconds <= Duration.ofDays(7).getSeconds()) {
            state = "EXPIRING_SOON";
        } else {
            state = "ACTIVE";
        }
        return new MemoryTtlView(
                "ABSOLUTE", state, localTimestamp(expiresAt), effectiveSeconds,
                remainingSeconds, remainingPercent, defaultDays,
                overrideDetected, false);
    }

    private static String localTimestamp(LocalDateTime value) {
        return value != null
                ? value.atZone(ZoneId.systemDefault()).toInstant().toString()
                : null;
    }

    private static String utcTimestamp(LocalDateTime value) {
        return value != null ? value.toInstant(ZoneOffset.UTC).toString() : null;
    }

    private boolean isBenchmarkMemory(MemoryEntryRow row) {
        String source = row.getSource() == null ? "" : row.getSource().toLowerCase(Locale.ROOT);
        String content = row.getContent() == null ? "" : row.getContent().toLowerCase(Locale.ROOT);
        return source.contains("benchmark") || content.contains("benchmark");
    }

    private boolean isControlFailureMemory(MemoryEntryRow row) {
        String content = row.getContent() == null ? "" : row.getContent().toUpperCase(Locale.ROOT);
        String source = row.getSource() == null ? "" : row.getSource().toUpperCase(Locale.ROOT);
        String type = row.getType() == null ? "" : row.getType();
        if (source.contains("CONTROL_PLANE") || source.contains("CONTROL_FAILURE")) {
            return true;
        }
        return content.contains("ORPHANED_ON_RESTART")
                || content.contains("RUNTIME_START_FAILED")
                || (("FAILURE".equalsIgnoreCase(type) || "SYSTEM".equalsIgnoreCase(type))
                && (content.contains("ORPHANED") || content.contains("RUNTIME_START_FAILED")));
    }

    private RagOpsResponse buildRagResponse(List<RagRetrievalView> items) {
        int volume = items.size();
        int successCount = (int) items.stream()
                .filter(item -> "SUCCESS".equals(item.outcome())).count();
        int terminalCount = (int) items.stream()
                .filter(item -> "SUCCESS".equals(item.outcome())
                        || "FAILED".equals(item.outcome())).count();
        int zeroHitCount = (int) items.stream()
                .filter(item -> Boolean.TRUE.equals(item.zeroHit())).count();
        int zeroHitEligibleCount = (int) items.stream()
                .filter(item -> item.zeroHit() != null).count();
        int errorCount = (int) items.stream()
                .filter(item -> "FAILED".equals(item.outcome())
                        || StringUtils.hasText(item.error())).count();
        int degradedCount = (int) items.stream()
                .filter(item -> Boolean.TRUE.equals(item.degraded())).count();
        int cacheHitCount = (int) items.stream()
                .filter(item -> Boolean.TRUE.equals(item.cacheHit())).count();
        int completeTelemetryCount = (int) items.stream()
                .filter(RagRetrievalView::telemetryComplete).count();

        List<Double> totalLatencies = items.stream()
                .map(item -> item.stages() != null ? item.stages().totalMs() : null)
                .filter(OpsDebugService::isFiniteNonNegative)
                .sorted()
                .toList();
        List<Double> topScores = items.stream()
                .map(OpsDebugService::normalizedRankingProxy)
                .filter(OpsDebugService::isFinite)
                .toList();
        List<Double> returned = items.stream()
                .map(RagRetrievalView::returnedK)
                .filter(v -> v != null)
                .map(Integer::doubleValue)
                .toList();
        List<Double> fillRatios = items.stream()
                .filter(item -> item.requestedK() != null && item.requestedK() > 0
                        && item.returnedK() != null)
                .map(item -> Math.min(1.0,
                        (double) item.returnedK() / item.requestedK()))
                .toList();
        List<Double> rerankLifts = items.stream()
                .map(RagRetrievalView::rerankLift)
                .filter(OpsDebugService::isFinite)
                .toList();

        List<RagStageAggregateView> stageBreakdown = List.of(
                stageAggregate("query_rewrite", items,
                        item -> item.stages() != null ? item.stages().queryRewriteMs() : null),
                stageAggregate("embedding", items,
                        item -> item.stages() != null ? item.stages().embeddingMs() : null),
                stageAggregate("retrieval", items,
                        item -> item.stages() != null ? item.stages().retrievalMs() : null),
                stageAggregate("embedding+retrieval", items,
                        item -> item.stages() != null ? item.stages().embeddingRetrievalMs() : null),
                stageAggregate("fusion", items,
                        item -> item.stages() != null ? item.stages().fusionMs() : null),
                stageAggregate("rerank", items,
                        item -> item.stages() != null ? item.stages().rerankMs() : null))
                .stream().filter(stage -> stage.samples() > 0).toList();
        RagStageAggregateView bottleneck = stageBreakdown.stream()
                .max(Comparator.comparing(stage ->
                        stage.averageMs() != null ? stage.averageMs() : -1D))
                .orElse(null);

        RagOpsSummary summary = new RagOpsSummary(
                volume,
                terminalCount,
                successCount,
                zeroHitCount,
                zeroHitEligibleCount,
                errorCount,
                degradedCount,
                cacheHitCount,
                rate(successCount, terminalCount),
                rate(zeroHitCount, zeroHitEligibleCount),
                percentile(totalLatencies, 0.50),
                percentile(totalLatencies, 0.90),
                average(topScores),
                average(returned),
                average(fillRatios),
                average(rerankLifts),
                rerankLifts.size(),
                bottleneck != null ? bottleneck.stage() : null,
                bottleneck != null ? bottleneck.averageMs() : null,
                stageBreakdown,
                completeTelemetryCount);

        Map<String, Object> semantics = new LinkedHashMap<>();
        semantics.put("rankingScores",
                "final/reranker scores are ranking proxies; legacy raw RRF values are normalized by the two-channel theoretical maximum for summary display, never interpreted as precision or recall");
        semantics.put("topKFillRate",
                "returnedK/requestedK is a capacity proxy; it does not prove relevance");
        semantics.put("precisionRecall",
                "reported only when the event explicitly references a labelled ground-truth set");
        semantics.put("groundedness",
                "reported only when a named judge completed successfully");
        semantics.put("missingValues",
                "null means the runtime did not collect that field; zero is never substituted");

        List<String> warnings = new ArrayList<>();
        if (items.isEmpty()) {
            warnings.add("No retrieval invocation events matched the current filters.");
        } else if (completeTelemetryCount < items.size()) {
            warnings.add((items.size() - completeTelemetryCount)
                    + " invocation(s) use legacy or partial telemetry; missing values remain null.");
        }
        if (items.stream().noneMatch(item -> item.quality() != null
                && item.quality().groundTruthAvailable())) {
            warnings.add("No labelled relevance set is attached; dashboard relevance values are proxies.");
        }
        return new RagOpsResponse(
                "rag-observability.v2",
                LocalDateTime.now(),
                items.size(),
                summary,
                items,
                semantics,
                warnings);
    }

    private RagStageAggregateView stageAggregate(
            String name,
            List<RagRetrievalView> items,
            java.util.function.Function<RagRetrievalView, Double> extractor) {
        List<Double> values = items.stream()
                .map(extractor)
                .filter(OpsDebugService::isFiniteNonNegative)
                .sorted()
                .toList();
        List<Double> shares = new ArrayList<>();
        for (RagRetrievalView item : items) {
            Double stage = extractor.apply(item);
            if (!isFiniteNonNegative(stage) || item.stages() == null) {
                continue;
            }
            double knownTotal = sumKnownStages(item.stages());
            if (knownTotal > 0) {
                shares.add(stage / knownTotal);
            }
        }
        return new RagStageAggregateView(
                name, values.size(), average(values), percentile(values, 0.90), average(shares));
    }

    private static Double normalizedRankingProxy(RagRetrievalView item) {
        Double score = item != null ? item.topScore() : null;
        if (!isFinite(score)) return null;
        String fusion = item.fusionStrategy() == null
                ? "" : item.fusionStrategy().toLowerCase(Locale.ROOT);
        if (!Boolean.TRUE.equals(item.rerankApplied())
                && fusion.contains("rrf") && score <= 0.05) {
            // k=60, two recall channels: max raw RRF = 2/(60+1).
            return Math.min(1.0, score / (2.0 / 61.0));
        }
        return score;
    }

    private static double sumKnownStages(RagStageTimingView stages) {
        double sum = 0D;
        for (Double value : List.of(
                valueOrZero(stages.queryRewriteMs()),
                valueOrZero(stages.embeddingMs()),
                valueOrZero(stages.retrievalMs()),
                valueOrZero(stages.embeddingRetrievalMs()),
                valueOrZero(stages.fusionMs()),
                valueOrZero(stages.rerankMs()))) {
            sum += value;
        }
        return sum;
    }

    private static double valueOrZero(Double value) {
        return isFiniteNonNegative(value) ? value : 0D;
    }

    private static Double rate(int numerator, int denominator) {
        return denominator > 0 ? (double) numerator / denominator : null;
    }

    private static Double average(List<Double> values) {
        if (values == null || values.isEmpty()) {
            return null;
        }
        return values.stream().mapToDouble(Double::doubleValue).average().orElse(Double.NaN);
    }

    private static Double percentile(List<Double> sortedValues, double quantile) {
        if (sortedValues == null || sortedValues.isEmpty()) {
            return null;
        }
        int index = (int) Math.ceil(quantile * sortedValues.size()) - 1;
        return sortedValues.get(Math.max(0, Math.min(index, sortedValues.size() - 1)));
    }

    private static boolean isFinite(Double value) {
        return value != null && Double.isFinite(value);
    }

    private static boolean isFiniteNonNegative(Double value) {
        return isFinite(value) && value >= 0D;
    }

    private static String ragSortTime(RagRetrievalView item) {
        if (StringUtils.hasText(item.endedAt())) {
            return item.endedAt();
        }
        if (StringUtils.hasText(item.occurredAt())) {
            return item.occurredAt();
        }
        return item.startedAt();
    }

    private RagDraft findCompatibleDraft(List<RagDraft> drafts, RunEvent event,
                                         String toolName, boolean wantsTelemetry,
                                         Map<String, Object> payload) {
        String incomingQuery = wantsTelemetry
                ? firstTextDeep(payload, "query", "queryText", "originalQuery") : null;
        for (int i = drafts.size() - 1; i >= 0; i--) {
            RagDraft draft = drafts.get(i);
            if (!sameText(draft.runId, event.getRunId())
                    || !sameText(draft.agentId, event.getAgentId())
                    || !sameText(draft.toolName, toolName)) {
                continue;
            }
            String draftQuery = firstTextDeep(draft.data,
                    "query", "queryText", "originalQuery");
            if (wantsTelemetry && StringUtils.hasText(incomingQuery)
                    && StringUtils.hasText(draftQuery)
                    && !incomingQuery.equals(draftQuery)) {
                continue;
            }
            if (wantsTelemetry ? !draft.hasRetrievalTelemetry : draft.endedAt == null) {
                return draft;
            }
        }
        return null;
    }

    private static String callKey(String runId, String toolCallId) {
        return String.valueOf(runId) + "|" + toolCallId;
    }

    private static boolean sameText(String left, String right) {
        return String.valueOf(left).equals(String.valueOf(right));
    }

    private final class RagDraft {
        private final String runId;
        private final String traceId;
        private final String agentId;
        private final String toolName;
        private Integer seq;
        private String toolCallId;
        private String occurredAt;
        private String startedAt;
        private String endedAt;
        private String retrievedAt;
        private Long durationMs;
        private String outcome = "RUNNING";
        private Boolean cacheHit;
        private String error;
        private boolean hasRetrievalTelemetry;
        private final Map<String, Object> data = new LinkedHashMap<>();

        private RagDraft(RunEvent event, String toolName, String toolCallId) {
            this.runId = event.getRunId();
            this.traceId = event.getTraceId();
            this.agentId = event.getAgentId();
            this.toolName = toolName;
            this.toolCallId = toolCallId;
            this.seq = event.getSeq();
            this.occurredAt = eventTime(event);
        }

        private void mergeStarted(RunEvent event, Map<String, Object> payload) {
            mergeData(payload);
            mergeData(asMap(payload.get("arguments")));
            seq = minSeq(seq, event.getSeq());
            String payloadTime = firstTextDeep(payload,
                    "startedAt", "startTime", "occurredAt", "timestamp");
            startedAt = firstNonBlank(payloadTime, eventTime(event));
            occurredAt = firstNonBlank(
                    firstTextDeep(payload, "occurredAt", "timestamp"),
                    occurredAt);
        }

        private void mergeCompleted(RunEvent event, Map<String, Object> payload, boolean failed) {
            mergeData(payload);
            Object preview = firstValueDeep(payload, "result", "resultPreview", "output");
            if (preview instanceof Map<?, ?> map) {
                mergeData(castObjectMap(map));
            } else if (preview instanceof String text) {
                Object parsed = parseJson(text);
                if (parsed instanceof Map<?, ?> map) {
                    mergeData(castObjectMap(map));
                }
            }
            String payloadTime = firstTextDeep(payload,
                    "endedAt", "completedAt", "endTime", "occurredAt", "timestamp");
            endedAt = firstNonBlank(payloadTime, eventTime(event));
            occurredAt = firstNonBlank(
                    firstTextDeep(payload, "occurredAt", "timestamp"),
                    occurredAt);
            durationMs = firstLongDeep(payload, "durationMs", "latencyMs");
            cacheHit = firstBooleanDeep(payload, "cacheHit");
            error = firstNonBlank(firstTextDeep(payload,
                    "error", "errorMessage", "failureReason"), error);
            Object resultSuccess = firstValueDeep(data, "success");
            outcome = failed || Boolean.FALSE.equals(resultSuccess)
                    ? "FAILED" : normalizeOutcome(firstTextDeep(payload, "outcome", "status"), "SUCCESS");
        }

        private void mergeRetrieval(RunEvent event, Map<String, Object> payload, boolean failed) {
            hasRetrievalTelemetry = true;
            mergeData(payload);
            String payloadTime = firstTextDeep(payload,
                    "occurredAt", "retrievedAt", "timestamp");
            retrievedAt = firstNonBlank(
                    firstTextDeep(payload, "retrievedAt", "retrievalTime"),
                    retrievedAt);
            occurredAt = firstNonBlank(payloadTime, eventTime(event));
            if (endedAt == null) {
                endedAt = firstNonBlank(
                        firstTextDeep(payload, "endedAt", "completedAt"),
                        eventTime(event));
            }
            if (durationMs == null) {
                durationMs = firstLongDeep(payload, "durationMs", "latencyMs");
            }
            cacheHit = firstNonNullBoolean(
                    firstBooleanDeep(payload, "cacheHit"), cacheHit);
            error = firstNonBlank(firstTextDeep(payload,
                    "error", "errorMessage", "failureReason"), error);
            if (failed || Boolean.FALSE.equals(firstValueDeep(payload, "success"))) {
                outcome = "FAILED";
            } else if (!"FAILED".equals(outcome)) {
                outcome = normalizeOutcome(firstTextDeep(payload, "outcome", "status"), "SUCCESS");
            }
            seq = maxSeq(seq, event.getSeq());
        }

        private void mergeData(Map<String, Object> source) {
            if (source == null) {
                return;
            }
            source.forEach((key, value) -> {
                if (meaningful(value)) {
                    data.put(key, value);
                }
            });
        }

        private RagRetrievalView toView() {
            String query = firstTextDeep(data, "query", "queryText", "originalQuery");
            List<String> queriesUsed = stringListDeep(data, "queriesUsed", "rewrittenQueries");
            if (queriesUsed.isEmpty() && StringUtils.hasText(query)) {
                queriesUsed = List.of(query);
            }

            List<RagChunkView> chunks = ragChunks(data);
            Integer requestedK = firstIntegerDeep(data,
                    "requestedK", "topK", "top_k", "limit");
            Integer returnedK = firstIntegerDeep(data,
                    "returnedK", "hitCount", "hitsReturned", "resultCount");
            Object rawChunkNode = firstValueDeep(data,
                    "chunks", "results", "hits", "items", "selectedChunks");
            if (returnedK == null && rawChunkNode instanceof List<?> list) {
                returnedK = list.size();
            }
            Integer uniqueDocuments = firstIntegerDeep(data,
                    "uniqueDocuments", "uniqueDocumentCount", "documentCount");
            if (uniqueDocuments == null && !chunks.isEmpty()) {
                long distinct = chunks.stream()
                        .map(RagChunkView::documentId)
                        .filter(StringUtils::hasText)
                        .distinct().count();
                uniqueDocuments = distinct > 0 ? (int) distinct : null;
            }

            List<Double> scoreSamples = chunks.stream()
                    .map(RagChunkView::score)
                    .filter(OpsDebugService::isFinite)
                    .toList();
            Double topScore = firstDoubleDeep(data,
                    "topRelevanceScore", "topScore", "maxScore", "afterTopScore", "top");
            if (topScore == null && !scoreSamples.isEmpty()) {
                topScore = scoreSamples.stream().max(Double::compareTo).orElse(null);
            }
            Double meanScore = firstDoubleDeep(data,
                    "meanScore", "averageScore", "avgScore", "mean");
            if (meanScore == null) {
                meanScore = average(scoreSamples);
            }
            Double minScore = firstDoubleDeep(data, "minScore", "min");
            if (minScore == null && !scoreSamples.isEmpty()) {
                minScore = scoreSamples.stream().min(Double::compareTo).orElse(null);
            }
            Double spread = firstDoubleDeep(data, "scoreSpread");
            if (spread == null && topScore != null && minScore != null) {
                spread = topScore - minScore;
            }

            RagStageTimingView stages = ragStages(data, durationMs);
            Long resolvedDuration = durationMs;
            if (resolvedDuration == null && stages.totalMs() != null) {
                resolvedDuration = Math.round(stages.totalMs());
            }

            Boolean rerankApplied = firstBooleanDeep(data,
                    "rerankApplied", "agenticRerank", "reranked");
            String strategy = firstTextDeep(data, "strategy", "retrievalStrategy");
            String fusion = firstTextDeep(data, "fusionStrategy", "fusion");
            if (rerankApplied == null && StringUtils.hasText(strategy)
                    && strategy.toLowerCase(Locale.ROOT).contains("rerank")) {
                rerankApplied = true;
            }
            Double beforeTop = firstDoubleDeep(data,
                    "rerankBeforeTopScore", "beforeTopScore");
            Double afterTop = firstDoubleDeep(data,
                    "rerankAfterTopScore", "afterTopScore");
            Double rerankLift = firstDoubleDeep(data, "rerankLift");
            if (rerankLift == null && beforeTop != null && afterTop != null) {
                rerankLift = afterTop - beforeTop;
            }
            String beforeTopChunkId = firstTextDeep(data,
                    "rerankBeforeTopChunkId", "beforeTopChunkId");
            String afterTopChunkId = firstTextDeep(data,
                    "rerankAfterTopChunkId", "afterTopChunkId");
            Integer rerankMovedCount = firstIntegerDeep(data,
                    "rerankMovedCount", "movedCount");

            Boolean fallback = firstBooleanDeep(data,
                    "fallback", "fallbackUsed", "usedFallback");
            String fallbackStage = firstTextDeep(data, "fallbackStage");
            List<String> fallbackChain = stringListDeep(data, "fallbackChain");
            Boolean degraded = firstBooleanDeep(data, "degraded");
            String degradationReason = firstTextDeep(data,
                    "degradationReason", "fallbackReason", "degradedReason", "errorType");
            if (degraded == null && (Boolean.TRUE.equals(fallback) || StringUtils.hasText(error))) {
                degraded = true;
            }
            if (fallback == null && StringUtils.hasText(fallbackStage)) {
                fallback = !"hybrid".equalsIgnoreCase(fallbackStage)
                        && !"vector".equalsIgnoreCase(fallbackStage);
            }

            Integer scoreSampleSize = firstIntegerDeep(
                    data, "scoreSampleSize", "collectedCount");
            if (scoreSampleSize == null && !scoreSamples.isEmpty()) {
                scoreSampleSize = scoreSamples.size();
            }
            RagQualityView quality = ragQuality(data);
            boolean telemetryComplete = hasRetrievalTelemetry
                    && requestedK != null
                    && returnedK != null
                    && StringUtils.hasText(strategy)
                    && stages.totalMs() != null;
            String resolvedRetrievedAt = firstNonBlank(
                    firstTextDeep(data, "retrievedAt", "retrievalTime"), retrievedAt);
            String resolvedOccurredAt = firstNonBlank(
                    firstTextDeep(data, "occurredAt", "timestamp"),
                    firstNonBlank(resolvedRetrievedAt,
                            firstNonBlank(endedAt, firstNonBlank(startedAt, occurredAt))));
            String resolvedStart = firstNonBlank(
                    firstTextDeep(data, "startedAt", "startTime"), startedAt);
            String resolvedEnd = firstNonBlank(
                    firstTextDeep(data, "endedAt", "completedAt", "endTime"), endedAt);
            Boolean zeroHit = returnedK != null ? returnedK == 0 : null;

            return new RagRetrievalView(
                    runId, traceId, seq, toolCallId, toolName, agentId,
                    query, truncate(query, 160), queriesUsed, outcome,
                    resolvedOccurredAt, resolvedStart, resolvedEnd, resolvedRetrievedAt,
                    resolvedDuration,
                    strategy, fusion,
                    firstTextDeep(data, "indexName", "index", "collectionName", "collection"),
                    firstTextDeep(data, "source", "sourceType", "backend"),
                    requestedK, returnedK, uniqueDocuments,
                    firstIntegerDeep(data, "candidateCount", "retrievedCandidateCount"),
                    firstIntegerDeep(data, "lexicalHits"),
                    firstIntegerDeep(data, "vectorHits"),
                    firstIntegerDeep(data, "filteredCount", "filterCount"),
                    firstIntegerDeep(data, "droppedCount", "dropCount"),
                    firstIntegerDeep(data, "deduplicatedCount", "dedupCount"),
                    zeroHit, topScore, meanScore, minScore, spread, scoreSampleSize,
                    rerankApplied, beforeTop, afterTop, rerankLift,
                    beforeTopChunkId, afterTopChunkId, rerankMovedCount,
                    cacheHit, fallback, fallbackStage, fallbackChain,
                    degraded, degradationReason, error, stages, chunks, quality,
                    telemetryComplete);
        }
    }

    private static RagStageTimingView ragStages(Map<String, Object> data, Long durationMs) {
        Object stageNode = firstValueDeep(data, "stageTimings", "stages", "latency");
        Map<String, Object> stages = asMap(stageNode);
        Double rewrite = firstDouble(stages, data,
                "queryRewriteMs", "query_rewrite_ms", "rewrite_ms", "rewriteMs");
        Double embedding = firstDouble(stages, data,
                "embeddingMs", "embedding_ms");
        Double retrieval = firstDouble(stages, data,
                "retrievalMs", "retrieveMs", "retrieval_ms", "retrieve_ms", "search_ms");
        Double embeddingRetrieval = firstDouble(stages, data,
                "embeddingRetrievalMs", "embedding_retrieval_ms",
                "embedding_search_ms", "embeddingSearchMs");
        Double fusion = firstDouble(stages, data,
                "fusionMs", "fusion_ms");
        Double rerank = firstDouble(stages, data,
                "rerankMs", "rerank_ms");
        Double total = firstDouble(stages, data,
                "totalMs", "total_ms", "pipelineMs", "pipeline_ms");
        if (total == null && durationMs != null) {
            total = durationMs.doubleValue();
        }
        return new RagStageTimingView(
                rewrite, embedding, retrieval, embeddingRetrieval, fusion, rerank, total);
    }

    private static List<RagChunkView> ragChunks(Map<String, Object> data) {
        Object raw = firstValueDeep(data,
                "chunks", "results", "hits", "items", "snippets", "selectedChunks");
        if (!(raw instanceof List<?> list)) {
            return List.of();
        }
        List<RagChunkView> chunks = new ArrayList<>();
        int fallbackRank = 0;
        for (Object item : list) {
            fallbackRank++;
            if (item instanceof String text) {
                chunks.add(new RagChunkView(
                        null, null, null, null, null, null, null,
                        fallbackRank, truncate(text, 500), null));
                continue;
            }
            if (!(item instanceof Map<?, ?> rawMap)) {
                continue;
            }
            Map<String, Object> map = castObjectMap(rawMap);
            Object provenance = firstValueDeep(map, "provenance", "metadata");
            Double score = firstDoubleDeep(map,
                    "finalScore", "retrievalScore", "score", "relevanceScore",
                    "similarity", "rerankScore", "rrfScore", "vectorScore", "bm25Score");
            String scoreType = firstPresentKey(map,
                    "finalScore", "retrievalScore", "score", "relevanceScore",
                    "similarity", "rerankScore", "rrfScore", "vectorScore", "bm25Score");
            Integer rank = firstIntegerDeep(map, "rank", "rankNo", "position");
            chunks.add(new RagChunkView(
                    firstTextDeep(map, "chunkId", "segmentId", "id"),
                    firstTextDeep(map, "documentId", "docId"),
                    firstTextDeep(map, "title", "documentTitle"),
                    firstTextDeep(map, "source", "sourceType", "channel"),
                    firstTextDeep(map, "sourceUri", "uri", "url"),
                    score,
                    scoreType,
                    rank != null ? rank : fallbackRank,
                    truncate(firstTextDeep(map,
                            "preview", "content", "text", "pageContent", "snippet"), 500),
                    provenance));
        }
        return chunks;
    }

    private static RagQualityView ragQuality(Map<String, Object> data) {
        Map<String, Object> quality = asMap(firstValueDeep(data,
                "quality", "qualityMetrics", "evaluation"));
        String groundTruthProvenance = firstText(quality, data,
                "groundTruthDatasetId", "labelSetId", "relevanceLabelSetId",
                "groundTruthId", "groundTruthProvenance");
        String groundTruthStatus = firstText(quality, data,
                "groundTruthStatus", "groundTruthEvaluationStatus",
                "labelSetStatus", "labelEvaluationStatus", "evaluationStatus");
        boolean groundTruthAvailable = StringUtils.hasText(groundTruthProvenance)
                && isSuccessfulEvaluationStatus(groundTruthStatus);
        String judgeSource = firstText(quality, data,
                "judgeSource", "judgeName", "evaluator");
        String judgeStatus = firstText(quality, data, "judgeStatus", "evaluationStatus");
        boolean judgeCompleted = StringUtils.hasText(judgeSource)
                && isSuccessfulEvaluationStatus(judgeStatus);
        Double precisionAtK = groundTruthAvailable
                ? firstDouble(quality, data, "precisionAtK", "precision_at_k")
                : null;
        Double recallAtK = groundTruthAvailable
                ? firstDouble(quality, data, "recallAtK", "recall_at_k")
                : null;
        Double groundedness = judgeCompleted
                ? firstDouble(quality, data, "groundedness", "faithfulness")
                : null;
        String note;
        if (groundTruthAvailable || judgeCompleted) {
            note = "Only explicitly labelled/judged metrics are shown; ranking scores remain proxies.";
        } else {
            note = "No labelled relevance set or completed judge is attached; precision, recall and groundedness are not reported.";
        }
        return new RagQualityView(
                groundTruthAvailable,
                judgeSource,
                precisionAtK,
                recallAtK,
                groundedness,
                "retriever_or_reranker_score_proxy",
                note);
    }

    private static boolean isSuccessfulEvaluationStatus(String status) {
        return StringUtils.hasText(status)
                && Set.of("SUCCESS", "SUCCEEDED", "COMPLETED", "OK")
                .contains(status.trim().toUpperCase(Locale.ROOT));
    }

    private static Double firstDouble(Map<String, Object> preferred,
                                      Map<String, Object> fallback,
                                      String... keys) {
        Double value = firstDoubleDeep(preferred, keys);
        return value != null ? value : firstDoubleDeep(fallback, keys);
    }

    private static String firstText(Map<String, Object> preferred,
                                    Map<String, Object> fallback,
                                    String... keys) {
        String value = firstTextDeep(preferred, keys);
        return StringUtils.hasText(value) ? value : firstTextDeep(fallback, keys);
    }

    private static Boolean firstBoolean(Map<String, Object> preferred,
                                        Map<String, Object> fallback,
                                        String... keys) {
        Boolean value = firstBooleanDeep(preferred, keys);
        return value != null ? value : firstBooleanDeep(fallback, keys);
    }

    private static String firstText(Map<String, Object> map, String... keys) {
        return firstTextDeep(map, keys);
    }

    private static Object firstValueDeep(Object root, String... keys) {
        for (String key : keys) {
            Object found = findKey(root, key, 0);
            if (found != null) {
                return found;
            }
        }
        return null;
    }

    private static Object findKey(Object node, String key, int depth) {
        if (!(node instanceof Map<?, ?> map) || depth > 4) {
            return null;
        }
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (key.equalsIgnoreCase(String.valueOf(entry.getKey()))
                    && meaningful(entry.getValue())) {
                return entry.getValue();
            }
        }
        for (Object value : map.values()) {
            if (value instanceof Map<?, ?>) {
                Object nested = findKey(value, key, depth + 1);
                if (nested != null) {
                    return nested;
                }
            }
        }
        return null;
    }

    private static String firstTextDeep(Object root, String... keys) {
        Object value = firstValueDeep(root, keys);
        if (value == null || value instanceof Map<?, ?> || value instanceof List<?>) {
            return null;
        }
        String text = String.valueOf(value).trim();
        return text.isEmpty() ? null : text;
    }

    private static Integer firstIntegerDeep(Object root, String... keys) {
        Object value = firstValueDeep(root, keys);
        return intOrNull(value);
    }

    private static Long firstLongDeep(Object root, String... keys) {
        Object value = firstValueDeep(root, keys);
        return longOrNull(value);
    }

    private static Double firstDoubleDeep(Object root, String... keys) {
        Object value = firstValueDeep(root, keys);
        if (value instanceof Number number) {
            double parsed = number.doubleValue();
            return Double.isFinite(parsed) ? parsed : null;
        }
        if (value == null) {
            return null;
        }
        try {
            double parsed = Double.parseDouble(String.valueOf(value));
            return Double.isFinite(parsed) ? parsed : null;
        } catch (Exception ignored) {
            return null;
        }
    }

    private static Boolean firstBooleanDeep(Object root, String... keys) {
        Object value = firstValueDeep(root, keys);
        if (value instanceof Boolean flag) {
            return flag;
        }
        if (value == null) {
            return null;
        }
        String text = String.valueOf(value).trim();
        if ("true".equalsIgnoreCase(text) || "1".equals(text)) {
            return true;
        }
        if ("false".equalsIgnoreCase(text) || "0".equals(text)) {
            return false;
        }
        return null;
    }

    private static List<String> stringListDeep(Object root, String... keys) {
        Object value = firstValueDeep(root, keys);
        if (value instanceof List<?> list) {
            return list.stream()
                    .filter(item -> item != null && StringUtils.hasText(String.valueOf(item)))
                    .map(String::valueOf)
                    .toList();
        }
        if (value instanceof String text && StringUtils.hasText(text)) {
            return List.of(text);
        }
        return List.of();
    }

    private static String firstPresentKey(Map<String, Object> map, String... keys) {
        for (String key : keys) {
            Object value = findKey(map, key, 0);
            if (value != null) {
                return key;
            }
        }
        return null;
    }

    private static Map<String, Object> asMap(Object raw) {
        if (raw instanceof Map<?, ?> map) {
            return castObjectMap(map);
        }
        return Map.of();
    }

    private static boolean meaningful(Object value) {
        if (value == null) {
            return false;
        }
        if (value instanceof String text) {
            return !text.isBlank();
        }
        if (value instanceof Map<?, ?> map) {
            return !map.isEmpty();
        }
        if (value instanceof List<?> list) {
            return !list.isEmpty();
        }
        return true;
    }

    private static String normalizeOutcome(String raw, String fallback) {
        if (!StringUtils.hasText(raw)) {
            return fallback;
        }
        String normalized = raw.trim().toUpperCase(Locale.ROOT);
        if (Set.of("SUCCESS", "SUCCEEDED", "COMPLETED", "OK").contains(normalized)) {
            return "SUCCESS";
        }
        if (Set.of("FAILED", "FAILURE", "ERROR", "TIMED_OUT", "TIMEOUT").contains(normalized)) {
            return "FAILED";
        }
        if (Set.of("RUNNING", "STARTED", "PENDING").contains(normalized)) {
            return "RUNNING";
        }
        return fallback;
    }

    private static Boolean firstNonNullBoolean(Boolean first, Boolean second) {
        return first != null ? first : second;
    }

    private static String firstNonBlank(String first, String second) {
        return StringUtils.hasText(first) ? first : second;
    }

    private static String firstNonBlank(String first, String second, String fallback) {
        String value = firstNonBlank(first, second);
        return StringUtils.hasText(value) ? value : fallback;
    }

    private static String eventTime(RunEvent event) {
        return event != null && event.getCreateTime() != null
                ? event.getCreateTime().toString() : null;
    }

    private static Integer minSeq(Integer left, Integer right) {
        if (left == null) return right;
        if (right == null) return left;
        return Math.min(left, right);
    }

    private static Integer maxSeq(Integer left, Integer right) {
        if (left == null) return right;
        if (right == null) return left;
        return Math.max(left, right);
    }

    private static List<String> castStringList(Object raw) {
        if (!(raw instanceof List<?> list)) {
            return List.of();
        }
        List<String> out = new ArrayList<>();
        for (Object item : list) {
            if (item != null) {
                out.add(String.valueOf(item));
            }
        }
        return out;
    }

    private static List<List<String>> castGroups(Object raw) {
        if (!(raw instanceof List<?> list)) {
            return List.of();
        }
        List<List<String>> out = new ArrayList<>();
        for (Object group : list) {
            out.add(castStringList(group));
        }
        return out;
    }

    private static Map<String, Object> castObjectMap(Map<?, ?> raw) {
        Map<String, Object> out = new LinkedHashMap<>();
        raw.forEach((k, v) -> out.put(String.valueOf(k), v));
        return out;
    }

    private static String str(Object value) {
        return value == null ? null : String.valueOf(value);
    }

    private static int intOf(Object value) {
        Integer n = intOrNull(value);
        return n != null ? n : 0;
    }

    private static Integer intOrNull(Object value) {
        if (value instanceof Number n) {
            return n.intValue();
        }
        if (value == null) {
            return null;
        }
        try {
            return Integer.parseInt(String.valueOf(value));
        } catch (Exception e) {
            return null;
        }
    }

    private static Long longOrNull(Object value) {
        if (value instanceof Number n) {
            return n.longValue();
        }
        if (value == null) {
            return null;
        }
        try {
            return Long.parseLong(String.valueOf(value));
        } catch (Exception e) {
            return null;
        }
    }

    private static String truncate(String value, int max) {
        if (value == null) {
            return null;
        }
        String normalized = value.replaceAll("\\s+", " ").trim();
        return normalized.length() <= max ? normalized : normalized.substring(0, max) + "…";
    }
}
