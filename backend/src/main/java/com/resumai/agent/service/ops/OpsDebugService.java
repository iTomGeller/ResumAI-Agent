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
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpInvocationPage;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpInvocationView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryUsageView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.ObservabilityView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.PlanDebugView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugDetailResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugSummary;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillAggUsage;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillManifestItem;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillUsageView;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.TimelineEventView;
import com.resumai.agent.config.LangfuseHealthService;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.MemoryEntryMapper;
import com.resumai.agent.dao.RunEventMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import com.resumai.agent.domain.entity.RunEvent;
import com.resumai.agent.domain.entity.RunMemoryUsageRow;
import com.resumai.agent.service.AgentMemoryService;
import com.resumai.agent.service.RunMemoryUsageService;
import com.resumai.agent.service.run.AgentRuntimeClient;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
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
            "skill.selected", "skill.applied", "skill.failed",
            // Legacy aliases for historical rows only.
            "skill.started", "skill.completed");

    public static final List<String> TOOL_EVENT_TYPES = List.of(
            "tool.started", "tool.completed", "tool.failed");

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
    private final LangfuseHealthService langfuseHealth;
    private final ObjectMapper objectMapper;

    public OpsDebugService(AgentRunMapper agentRunMapper,
                           RunEventMapper runEventMapper,
                           MemoryEntryMapper memoryEntryMapper,
                           RunMemoryUsageService memoryUsageService,
                           AgentMemoryService agentMemoryService,
                           AgentRuntimeClient runtimeClient,
                           LangfuseHealthService langfuseHealth,
                           ObjectMapper objectMapper) {
        this.agentRunMapper = agentRunMapper;
        this.runEventMapper = runEventMapper;
        this.memoryEntryMapper = memoryEntryMapper;
        this.memoryUsageService = memoryUsageService;
        this.agentMemoryService = agentMemoryService;
        this.runtimeClient = runtimeClient;
        this.langfuseHealth = langfuseHealth;
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
            if (TOOL_EVENT_TYPES.contains(type) && isMcpPayload(payload)) {
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
                new ObservabilityView(langfuseHealth.snapshot()),
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
        List<McpInvocationView> calls = recentMcpCalls(recentLimit, runId, server, outcome);
        return new McpOpsResponse(
                inventory,
                new McpInvocationPage(calls.size(), calls),
                List.of("AVAILABLE", "RATE_LIMITED", "AUTH_REQUIRED", "DOWN", "UNREACHABLE"),
                "状态来自 Python MCP Registry 真实 probe；调用证据来自 run_event。");
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
        List<SkillAggUsage> bySkill = (List<SkillAggUsage>) usage.get("bySkill");
        return new SkillOpsResponse(
                source, reachable, root, count, activeCount, deprecatedCount, advertised,
                manifest, events, bySkill, runtimeError,
                "默认展示 Python runtime ACTIVE skills；选择/应用来自 run_event。");
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
        return new MemoryOpsResponse(
                entries.size(), skipped, byType, byScope, bySource, entries, usage,
                Map.of("hideBenchmark", !includeBenchmark, "hideControlFailure", !includeControlFailure),
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
                run.getCurrentAgent(), run.getCurrentTool(), run.getCurrentPhase(), run.getPolicyId(),
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
        item.put("policyId", s.policyId());
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

    public String sandboxPurpose(String purpose) {
        if (StringUtils.hasText(purpose)) {
            return purpose.trim();
        }
        return "LEGACY_CANDIDATE_EVALUATION";
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
                run.getSourceTaskTraceId(), run.getRevisionNo(), run.getPolicyId(),
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
                str(map.get("policyId")),
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
        for (RunMemoryUsageRow row : rows) {
            if (row.getMemoryId() != null && !byId.containsKey(row.getMemoryId())) {
                MemoryEntryRow entry = memoryEntryMapper.selectById(row.getMemoryId());
                if (entry != null) {
                    byId.put(row.getMemoryId(), entry);
                }
            }
        }
        List<MemoryUsageView> out = new ArrayList<>();
        for (RunMemoryUsageRow row : rows) {
            MemoryEntryRow entry = byId.get(row.getMemoryId());
            out.add(new MemoryUsageView(
                    row.getId(), row.getRunId(), row.getMemoryId(), row.getConsumerAgent(),
                    row.getRankNo(),
                    row.getVectorScore() != null ? row.getVectorScore().doubleValue() : null,
                    row.getLexicalScore() != null ? row.getLexicalScore().doubleValue() : null,
                    row.getRecencyScore() != null ? row.getRecencyScore().doubleValue() : null,
                    row.getFinalScore() != null ? row.getFinalScore().doubleValue() : null,
                    row.getDecision(), row.getIgnoredReason(), row.getCreateTime(),
                    entry != null ? entry.getType() : null,
                    entry != null ? entry.getOwnerScope() : null,
                    entry != null ? entry.getSource() : null,
                    entry != null ? truncate(entry.getContent(), 200) : null));
        }
        return out;
    }

    private McpInventory loadMcpInventory(boolean probe) {
        Optional<Map<String, Object>> runtime = runtimeClient.getOpsRuntime(probe);
        if (runtime.isEmpty()) {
            return new McpInventory("runtime_unreachable", false, false, null, List.of(), 0, null,
                    List.of(), "Python /internal/ops/runtime unavailable");
        }
        Object mcpNode = runtime.get().get("mcp");
        if (!(mcpNode instanceof Map<?, ?> mcpMap)) {
            return new McpInventory("python_mcp_registry", true, probe, null, List.of(), 0, null,
                    List.of(), null);
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> mcp = (Map<String, Object>) mcpMap;
        List<McpInventoryServer> servers = flattenMcpServers(mcp.get("servers"));
        return new McpInventory(
                String.valueOf(mcp.getOrDefault("source", "python_mcp_registry")),
                true,
                Boolean.TRUE.equals(mcp.get("probed")) || probe,
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

    private List<McpInvocationView> recentMcpCalls(int limit, String runId, String server, String outcome) {
        int cap = Math.max(1, Math.min(limit, 200));
        QueryWrapper<RunEvent> q = new QueryWrapper<RunEvent>()
                .in("event_type", TOOL_EVENT_TYPES)
                .orderByDesc("create_time")
                .last("limit " + Math.min(cap * 5, 500));
        if (StringUtils.hasText(runId)) {
            q.eq("run_id", runId.trim());
        }
        List<RunEvent> events = runEventMapper.selectList(q);
        List<McpInvocationView> out = new ArrayList<>();
        for (RunEvent event : events) {
            Object payload = parseJson(event.getPayload());
            if (!isMcpPayload(payload)) {
                continue;
            }
            McpInvocationView view = toMcpInvocation(event, payload);
            if (StringUtils.hasText(server) && (view.server() == null
                    || !server.equalsIgnoreCase(view.server()))) {
                continue;
            }
            if (StringUtils.hasText(outcome) && (view.outcome() == null
                    || !outcome.equalsIgnoreCase(view.outcome()))) {
                continue;
            }
            out.add(view);
            if (out.size() >= cap) {
                break;
            }
        }
        return out;
    }

    private McpInvocationView toMcpInvocation(RunEvent event, Object payload) {
        Map<?, ?> p = payload instanceof Map<?, ?> map ? map : Map.of();
        String type = event.getEventType() == null ? "" : event.getEventType();
        String outcome = type.endsWith("failed") ? "FAILED"
                : type.endsWith("completed") ? "SUCCESS" : "RUNNING";
        return new McpInvocationView(
                event.getRunId(), event.getTraceId(), event.getSeq(),
                str(p.get("toolCallId")),
                str(p.get("mcpServer")),
                event.getToolName(),
                event.getAgentId(),
                outcome,
                longOrNull(p.get("durationMs")),
                intOrNull(p.get("retryCount")),
                p.get("cacheHit") instanceof Boolean b ? b : null,
                redact(p.get("arguments")),
                redact(p.get("resultPreview")),
                str(redact(p.get("error"))),
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
        return new SkillUsageView(
                event.getRunId(), skillId, event.getAgentId(), event.getEventType(),
                str(p.get("triggerReason")),
                str(p.get("skillVersion")),
                runHash, runHash, manifestHash, drift, required, payload, event.getCreateTime());
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
        int cap = Math.max(1, Math.min(limit, 200));
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
            long selected = agg != null ? agg.selected() : 0L;
            long applied = agg != null ? agg.applied() : 0L;
            long failed = agg != null ? agg.failed() : 0L;
            String type = event.getEventType() == null ? "" : event.getEventType();
            if ("skill.selected".equals(type) || "skill.started".equals(type)) {
                selected++;
            } else if ("skill.applied".equals(type) || "skill.completed".equals(type)) {
                applied++;
            } else if ("skill.failed".equals(type)) {
                failed++;
            }
            bySkill.put(skillId, new SkillAggUsage(
                    skillId, selected, applied, failed,
                    event.getRunId(), event.getCreateTime(),
                    view.skillHash(), view.skillVersion()));
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
        item.put("updateTime", row.getUpdateTime());
        item.put("createTime", row.getCreateTime());
        return item;
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
