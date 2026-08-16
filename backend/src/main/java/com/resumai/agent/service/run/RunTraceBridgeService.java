package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.TraceEventResponse;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.RunEvent;
import com.resumai.agent.service.MemoryService;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Renders unified run events (run_event) into the task-detail Trace views the
 * frontend already speaks: the flat trace list, the phased execution tree and
 * the live /sse/traces feed. This replaced the legacy agent_execution_trace
 * writer — the view is built from real events only, never from placeholders.
 */
@Service
public class RunTraceBridgeService {

    private static final Set<String> CONTROL_PLANE_ERRORS = Set.of(
            "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED", "START_STUCK");

    private final AgentRunMapper runMapper;
    private final RunEventService eventService;
    private final ObjectMapper objectMapper;

    public RunTraceBridgeService(AgentRunMapper runMapper,
                                 RunEventService eventService,
                                 ObjectMapper objectMapper) {
        this.runMapper = runMapper;
        this.eventService = eventService;
        this.objectMapper = objectMapper;
    }

    /** Latest run whose traceId or bridged task trace matches. */
    public AgentRun findRunForTrace(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return null;
        }
        AgentRun byTask = runMapper.selectOne(new QueryWrapper<AgentRun>()
                .eq("source_task_trace_id", traceId)
                .orderByDesc("created_at").last("limit 1"));
        if (byTask != null) {
            return byTask;
        }
        return runMapper.selectOne(new QueryWrapper<AgentRun>()
                .eq("trace_id", traceId)
                .orderByDesc("created_at").last("limit 1"));
    }

    /** Prior attempts for the same source task / conversation trace. */
    public List<AgentRun> listAttemptRuns(String traceId, String currentRunId) {
        if (!StringUtils.hasText(traceId)) {
            return List.of();
        }
        List<AgentRun> byTask = runMapper.selectList(new QueryWrapper<AgentRun>()
                .eq("source_task_trace_id", traceId)
                .orderByAsc("created_at")
                .last("limit 50"));
        List<AgentRun> byTrace = runMapper.selectList(new QueryWrapper<AgentRun>()
                .eq("trace_id", traceId)
                .orderByAsc("created_at")
                .last("limit 50"));
        Map<String, AgentRun> merged = new LinkedHashMap<>();
        for (AgentRun run : byTask) {
            merged.put(run.getRunId(), run);
        }
        for (AgentRun run : byTrace) {
            merged.putIfAbsent(run.getRunId(), run);
        }
        List<AgentRun> out = new ArrayList<>();
        for (AgentRun run : merged.values()) {
            if (currentRunId != null && currentRunId.equals(run.getRunId())) {
                continue;
            }
            out.add(run);
        }
        return out;
    }

    // ------------------------------------------------------------------
    // Flat trace list (GET /api/traces/{traceId} fallback)
    // ------------------------------------------------------------------

    public List<TraceEventResponse> traceEventsForTrace(String traceId) {
        AgentRun run = findRunForTrace(traceId);
        if (run == null) {
            return List.of();
        }
        List<TraceEventResponse> out = new ArrayList<>();
        for (RunEvent event : eventService.listSince(run.getRunId(), 0, 2000)) {
            TraceEventResponse mapped = toTraceEvent(traceId, event);
            if (mapped != null) {
                out.add(mapped);
            }
        }
        return out;
    }

    /** One run event as a legacy trace row (used for list + live SSE). */
    public TraceEventResponse toTraceEvent(String traceId, RunEvent event) {
        Map<String, Object> payload = readPayload(event.getPayload());
        String type = event.getEventType() != null ? event.getEventType() : "";
        String agent = StringUtils.hasText(event.getAgentId())
                ? event.getAgentId() : agentless(type);
        String title;
        String detail;
        String status = "SUCCESS";
        Long duration = longOf(payload.get("durationMs"));
        Integer tokens = null;
        switch (type) {
            case "agent.selected" -> {
                title = "Coordinator 规划";
                detail = "计划: " + payload.getOrDefault("plan", List.of())
                        + "\n并行分组: " + payload.getOrDefault("parallelGroups", List.of())
                        + "\n依据: " + payload.getOrDefault("reason", "");
            }
            case "agent.started" -> {
                title = agent + " 开始";
                detail = String.valueOf(payload.getOrDefault("description", ""));
                status = "RUNNING";
            }
            case "agent.completed" -> {
                title = agent + " 完成";
                detail = String.valueOf(payload.getOrDefault("summary", ""));
            }
            case "agent.failed" -> {
                title = agent + " 失败";
                detail = String.valueOf(payload.getOrDefault("error", ""));
                status = "FAILED";
            }
            case "llm.started" -> {
                title = "LLM 调用 #" + payload.getOrDefault("callIndex", "");
                detail = "purpose=" + payload.getOrDefault("purpose", "");
                status = "RUNNING";
            }
            case "llm.completed" -> {
                title = "LLM 返回";
                int prompt = intOf(payload.get("promptTokens"));
                int completion = intOf(payload.get("completionTokens"));
                tokens = prompt + completion;
                detail = "tokens=" + prompt + "+" + completion
                        + " attempts=" + payload.getOrDefault("attempts", 1);
            }
            case "llm.context.attached" -> {
                title = "LLM 输入上下文已绑定";
                detail = "round=" + payload.getOrDefault("roundId", "")
                        + " memory=" + payload.getOrDefault("memoryCount", 0)
                        + " skills=" + payload.getOrDefault("skillCount", 0)
                        + " catalog=" + payload.getOrDefault("toolCatalogCount", 0);
            }
            case "llm.retrying" -> {
                title = "LLM 重试";
                detail = String.valueOf(payload.getOrDefault("error", ""));
                status = "RUNNING";
            }
            case "llm.failed" -> {
                title = "LLM 失败";
                detail = String.valueOf(payload.getOrDefault("error", ""));
                status = "FAILED";
            }
            case "tool.started" -> {
                title = "工具 " + event.getToolName();
                detail = "args=" + preview(payload.get("arguments"), 300);
                status = "RUNNING";
            }
            case "tool.completed" -> {
                title = "工具 " + event.getToolName() + " 完成";
                detail = preview(payload.get("resultPreview"), 400);
            }
            case "tool.failed" -> {
                title = "工具 " + event.getToolName() + " 失败";
                detail = String.valueOf(payload.getOrDefault("error", ""));
                status = "FAILED";
            }
            case "skill.catalog", "skill.catalog.exposed", "skill.selected",
                 "skill.loaded", "skill.applied", "skill.skipped", "skill.failed" -> {
                title = switch (type) {
                    case "skill.catalog", "skill.catalog.exposed" -> "技能元数据已暴露";
                    case "skill.selected" -> "技能选定";
                    case "skill.loaded" -> "技能指令渐进加载";
                    case "skill.applied" -> "技能已应用";
                    case "skill.skipped" -> "技能跳过";
                    default -> "技能失败";
                };
                detail = "skill=" + payload.getOrDefault("skillId", event.getToolName())
                        + "@" + payload.getOrDefault("skillVersion", "")
                        + " stage=" + payload.getOrDefault("lifecycleStage", "")
                        + " reason=" + payload.getOrDefault(
                                "reason", payload.getOrDefault("triggerReason", ""));
                status = "skill.failed".equals(type) ? "FAILED" : "SUCCESS";
            }
            case "skill.started", "skill.completed" -> {
                // Legacy fake tool lifecycle — treat as applied annotation.
                title = "技能 " + payload.getOrDefault("skillId", event.getToolName());
                detail = preview(payload, 300);
                status = "SUCCESS";
            }
            case "context.compacted" -> {
                title = "上下文压缩";
                detail = "tokens " + payload.getOrDefault("beforeTokens", "?")
                        + " → " + payload.getOrDefault("afterTokens", "?");
            }
            case "memory.read", "memory.selected", "memory.used", "memory.written",
                 "memory.skipped", "memory.missed" -> {
                String taxonomy = memoryTaxonomy(payload);
                title = switch (type) {
                    case "memory.read" -> "记忆检索";
                    case "memory.selected" -> "记忆命中";
                    case "memory.used" -> "记忆已用于 Agent 上下文";
                    case "memory.written" -> "记忆写入";
                    case "memory.skipped" -> "记忆跳过";
                    default -> "记忆未命中";
                };
                detail = "type=" + taxonomy
                        + " namespace=" + payload.getOrDefault("namespace", "unknown")
                        + " memoryId=" + payload.getOrDefault("memoryId", "")
                        + " score=" + payload.getOrDefault("score", "-")
                        + " reason=" + payload.getOrDefault("reason", "");
            }
            case "run.queued", "run.started", "run.progress", "run.cancelling",
                 "run.cancelled", "run.completed", "run.failed", "run.timed_out" -> {
                title = runTitle(type);
                detail = preview(payload, 300);
                status = type.contains("failed") || type.contains("timed_out")
                        ? "FAILED" : "SUCCESS";
            }
            default -> {
                title = type;
                detail = preview(payload, 300);
            }
        }
        String parentSpanId = causalParentSpan(type, payload);
        return new TraceEventResponse(
                traceId,
                "run-ev-" + event.getId(),
                parentSpanId,
                agent,
                mapEventKind(type),
                title,
                detail,
                status,
                duration,
                tokens,
                eventOccurredAt(payload, event.getCreateTime()));
    }

    // ------------------------------------------------------------------
    // Phased execution tree (GET /api/tasks/{traceId}/agent-execution fallback)
    // ------------------------------------------------------------------

    public Map<String, Object> executionTreeForTrace(String traceId) {
        AgentRun run = findRunForTrace(traceId);
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("traceId", traceId);
        tree.put("framework", "Unified Agent Runtime + DeepSeek");
        tree.put("architecture", "Coordinator 动态规划 + 并行 Specialist");
        if (run == null) {
            tree.put("executionTree", List.of());
            tree.put("historicalAttempts", List.of());
            return tree;
        }
        // Current attempt only — prior control-plane failures go to historicalAttempts.
        List<RunEvent> events = eventService.listSince(run.getRunId(), 0, 2000);

        Map<String, Integer> phaseOf = new LinkedHashMap<>();
        List<List<String>> groups = List.of();
        String planReason = "";
        int memoryHits = 0;
        List<Object> memoryTop = new ArrayList<>();
        Map<String, Object> argsByToolCall = new LinkedHashMap<>();
        Map<String, Map<String, Object>> proposalByToolCall = new LinkedHashMap<>();
        for (RunEvent event : events) {
            String type = event.getEventType();
            if ("agent.selected".equals(type)) {
                Map<String, Object> payload = readPayload(event.getPayload());
                groups = castGroups(payload.get("parallelGroups"));
                planReason = String.valueOf(payload.getOrDefault("reason", ""));
                memoryHits = intOf(payload.get("memoryHits"));
                for (int i = 0; i < groups.size(); i++) {
                    for (String agent : groups.get(i)) {
                        phaseOf.put(agent, i + 1);
                    }
                }
            } else if ("tool.started".equals(type)) {
                Map<String, Object> payload = readPayload(event.getPayload());
                Object callId = payload.get("toolCallId");
                if (callId != null && payload.get("arguments") != null) {
                    argsByToolCall.put(String.valueOf(callId), payload.get("arguments"));
                }
            } else if ("tool.progress".equals(type)) {
                Map<String, Object> payload = readPayload(event.getPayload());
                if ("LLM_PROPOSED".equals(payload.get("lifecycleStage"))
                        && payload.get("toolCallId") != null) {
                    proposalByToolCall.put(
                            String.valueOf(payload.get("toolCallId")), payload);
                }
            } else if ("run.progress".equals(type)) {
                Map<String, Object> payload = readPayload(event.getPayload());
                if ("memory".equals(payload.get("stage"))) {
                    memoryHits = intOf(payload.get("memoryHits"));
                    if (payload.get("memoryTop") instanceof List<?> top) {
                        memoryTop = canonicalMemoryRows(top, event);
                    }
                }
            }
        }

        Map<String, Map<String, Object>> nodes = new LinkedHashMap<>();
        Map<String, List<Map<String, Object>>> rounds = new LinkedHashMap<>();
        Map<String, Map<String, Map<String, Object>>> roundsById = new LinkedHashMap<>();
        Map<String, Map<Integer, Map<String, Object>>> roundsByCallIndex = new LinkedHashMap<>();
        Map<String, String> currentIterationRound = new LinkedHashMap<>();
        Map<String, Map<String, Object>> proposalRoundByAgent = new LinkedHashMap<>();
        Map<String, Integer> pendingProposalCountByAgent = new LinkedHashMap<>();
        Map<String, List<String>> pendingProposalNamesByAgent = new LinkedHashMap<>();
        Map<String, Map<String, Object>> toolByCall = new LinkedHashMap<>();
        Map<String, Map<String, Object>> toolRoundByCall = new LinkedHashMap<>();
        Map<String, List<Map<String, Object>>> deterministicSteps = new LinkedHashMap<>();
        Map<String, Set<String>> contextKeysByRound = new LinkedHashMap<>();
        int nextPhase = groups.size();
        for (RunEvent event : events) {
            String agent = event.getAgentId();
            if (!StringUtils.hasText(agent) || "CoordinatorAgent".equals(agent)) {
                continue;
            }
            Map<String, Object> payload = readPayload(event.getPayload());
            Map<String, Object> node = nodes.get(agent);
            if (node == null) {
                node = new LinkedHashMap<>();
                node.put("name", agent);
                node.put("role", "");
                node.put("phase", phaseOf.getOrDefault(agent, ++nextPhase));
                node.put("nodeId", agent);
                node.put("status", "RUNNING");
                node.put("durationMs", 0L);
                nodes.put(agent, node);
                rounds.put(agent, new ArrayList<>());
                roundsById.put(agent, new LinkedHashMap<>());
                roundsByCallIndex.put(agent, new LinkedHashMap<>());
                deterministicSteps.put(agent, new ArrayList<>());
            }
            switch (event.getEventType()) {
                case "agent.started" -> {
                    node.put("role", payload.getOrDefault("description", ""));
                    node.put("startedAt", temporalValue(
                            payload, "startedAt", "startAt", "occurredAt",
                            event.getCreateTime()));
                }
                case "agent.completed" -> {
                    node.put("status", "SUCCESS");
                    node.put("durationMs", longOf(payload.get("durationMs")));
                    node.put("output", payload.getOrDefault("summary", ""));
                    node.put("endedAt", temporalValue(
                            payload, "endedAt", "endAt", "occurredAt",
                            event.getCreateTime()));
                    int llmCalls = intOf(payload.get("llmCalls"));
                    node.put("llmCalls", llmCalls);
                    node.put("toolCalls", payload.getOrDefault("toolCalls", 0));
                    node.put("confidence", payload.getOrDefault("confidence", null));
                    if (llmCalls == 0) {
                        node.put("executionMode", "deterministic");
                    }
                }
                case "agent.failed" -> {
                    node.put("status", "FAILED");
                    node.put("durationMs", longOf(payload.get("durationMs")));
                    node.put("output", payload.getOrDefault("error", ""));
                }
                case "agent.progress" -> {
                    int iteration = intOf(payload.get("iteration"));
                    if (iteration > 0) {
                        String logicalRoundId = firstText(payload,
                                "roundId", "parentRoundId", "parentSpanId");
                        if (!StringUtils.hasText(logicalRoundId)) {
                            logicalRoundId = agent + "-iteration-" + iteration;
                        }
                        currentIterationRound.put(agent, logicalRoundId);
                    }
                }
                case "llm.context.attached" -> {
                    String roundId = causalRoundId(payload, agent,
                            currentIterationRound.get(agent));
                    if (!StringUtils.hasText(roundId)) {
                        break;
                    }
                    Map<String, Object> round = roundFor(
                            agent, roundId, payload, event,
                            rounds, roundsById, roundsByCallIndex);
                    round.put("contextRole", payload.getOrDefault(
                            "contextRole", "MODEL_INPUT"));
                    round.put("contextAttachedAt", eventInstant(payload, event));
                    copyIfPresent(round, payload,
                            "memoryCount", "skillCount", "toolCatalogCount",
                            "contextTokenEstimate", "promptHash",
                            "observedToolCallIds");
                    attachContextReferences(
                            round, payload, event,
                            contextKeysByRound.computeIfAbsent(
                                    agent + "|" + roundId, ignored -> new java.util.LinkedHashSet<>()));
                }
                case "llm.started" -> {
                    String roundId = causalRoundId(payload, agent,
                            currentIterationRound.get(agent));
                    if (!StringUtils.hasText(roundId)) {
                        roundId = agent + "-llm-" + intOf(payload.get("callIndex"));
                    }
                    Map<String, Object> round = roundFor(
                            agent, roundId, payload, event,
                            rounds, roundsById, roundsByCallIndex);
                    round.put("roundId", roundId);
                    round.put("status", "RUNNING");
                    round.put("purpose", payload.getOrDefault("purpose", ""));
                    round.put("model", payload.getOrDefault("model", ""));
                    round.put("startedAt", eventStart(payload, event));
                    round.putIfAbsent("occurredAt", eventStart(payload, event));
                }
                case "llm.completed" -> {
                    int prompt = intOf(payload.get("promptTokens"));
                    int completion = intOf(payload.get("completionTokens"));
                    String roundId = causalRoundId(payload, agent,
                            currentIterationRound.get(agent));
                    Map<String, Object> round = findRound(
                            agent, roundId, intOf(payload.get("callIndex")),
                            roundsById, roundsByCallIndex);
                    if (round == null) {
                        if (!StringUtils.hasText(roundId)) {
                            roundId = agent + "-llm-" + intOf(payload.get("callIndex"));
                        }
                        round = roundFor(agent, roundId, payload, event,
                                rounds, roundsById, roundsByCallIndex);
                    }
                    round.put("tokens", prompt + completion);
                    round.put("durationMs", longOf(payload.get("durationMs")));
                    round.put("model", payload.getOrDefault("model", ""));
                    round.put("status", "SUCCESS");
                    round.put("endedAt", eventEnd(payload, event));
                    round.putIfAbsent("occurredAt", round.getOrDefault(
                            "startedAt", eventInstant(payload, event)));
                    int toolCallCount = intOf(payload.get("toolCallCount"));
                    if (toolCallCount > 0) {
                        proposalRoundByAgent.put(agent, round);
                        pendingProposalCountByAgent.put(agent, toolCallCount);
                        pendingProposalNamesByAgent.put(
                                agent, stringList(payload.get("toolNames")));
                    }
                }
                case "llm.retrying", "llm.failed" -> {
                    String roundId = causalRoundId(payload, agent,
                            currentIterationRound.get(agent));
                    Map<String, Object> round = findRound(
                            agent, roundId, intOf(payload.get("callIndex")),
                            roundsById, roundsByCallIndex);
                    if (round == null) {
                        if (!StringUtils.hasText(roundId)) {
                            roundId = agent + "-llm-" + intOf(payload.get("callIndex"));
                        }
                        round = roundFor(agent, roundId, payload, event,
                                rounds, roundsById, roundsByCallIndex);
                    }
                    round.put("title", "llm.failed".equals(event.getEventType())
                            ? "LLM 失败" : "LLM 重试");
                    round.put("error", payload.getOrDefault("error", ""));
                    round.put("status", "llm.failed".equals(event.getEventType())
                            ? "FAILED" : "RUNNING");
                    round.put("endedAt", eventEnd(payload, event));
                }
                case "tool.progress", "tool.started", "tool.completed", "tool.failed" -> {
                    String toolCallId = firstText(payload,
                            "toolCallId", "callId", "invocationId");
                    if (!StringUtils.hasText(toolCallId)) {
                        break;
                    }
                    String callKey = agent + "|" + toolCallId;
                    String roundId = causalRoundId(payload, agent, null);
                    Map<String, Object> parentRound = findRound(
                            agent, roundId, intOf(payload.get("callIndex")),
                            roundsById, roundsByCallIndex);
                    String lifecycleStage = stringOf(payload.get("lifecycleStage"));
                    if (parentRound == null && "LLM_PROPOSED".equals(lifecycleStage)) {
                        Map<String, Object> candidate = proposalRoundByAgent.get(agent);
                        List<String> expectedNames = pendingProposalNamesByAgent.getOrDefault(
                                agent, List.of());
                        String modelName = firstText(payload, "modelName", "toolName");
                        int remaining = pendingProposalCountByAgent.getOrDefault(agent, 0);
                        if (candidate != null && remaining > 0
                                && (expectedNames.isEmpty() || expectedNames.contains(modelName))) {
                            parentRound = candidate;
                            pendingProposalCountByAgent.put(agent, remaining - 1);
                        }
                    }
                    if (parentRound == null) {
                        parentRound = toolRoundByCall.get(callKey);
                    }
                    Map<String, Object> tool = toolByCall.computeIfAbsent(
                            callKey, ignored -> newTool(event, payload, toolCallId));
                    mergeToolEvent(tool, event, payload,
                            argsByToolCall.get(toolCallId),
                            proposalByToolCall.get(toolCallId));
                    if (parentRound != null) {
                        deterministicSteps.get(agent).remove(tool);
                        attachTool(parentRound, tool);
                        toolRoundByCall.put(callKey, parentRound);
                        tool.put("parentRoundId", parentRound.get("roundId"));
                    } else if (!"CATALOG_EXPOSED".equals(lifecycleStage)) {
                        attachDeterministicStep(deterministicSteps.get(agent), tool);
                    }
                }
                case "skill.catalog", "skill.catalog.exposed", "skill.selected" -> {
                    // Metadata lifecycle remains available in the flat audit feed.
                    // It is not executable work and therefore is never a tree sibling.
                }
                case "skill.loaded", "skill.failed" -> {
                    String toolCallId = firstText(payload, "toolCallId", "callId");
                    if (!StringUtils.hasText(toolCallId)) {
                        break;
                    }
                    String callKey = agent + "|" + toolCallId;
                    Map<String, Object> tool = toolByCall.computeIfAbsent(
                            callKey, ignored -> newTool(event, payload, toolCallId));
                    mergeSkillLifecycle(tool, event, payload);
                    String proposalRoundId = firstText(payload,
                            "proposalRoundId", "parentRoundId", "roundId", "parentSpanId");
                    Map<String, Object> parentRound = findRound(
                            agent, proposalRoundId, 0, roundsById, roundsByCallIndex);
                    if (parentRound == null) {
                        parentRound = toolRoundByCall.get(callKey);
                    }
                    if (parentRound != null) {
                        deterministicSteps.get(agent).remove(tool);
                        attachTool(parentRound, tool);
                        toolRoundByCall.put(callKey, parentRound);
                        tool.put("parentRoundId", parentRound.get("roundId"));
                    }
                }
                case "skill.applied" -> {
                    String applicationRoundId = firstText(payload,
                            "applicationRoundId", "parentRoundId", "roundId", "parentSpanId");
                    Map<String, Object> applicationRound = findRound(
                            agent, applicationRoundId, intOf(payload.get("callIndex")),
                            roundsById, roundsByCallIndex);
                    if (applicationRound != null) {
                        attachSkillContext(
                                applicationRound, payload, event,
                                contextKeysByRound.computeIfAbsent(
                                        agent + "|" + applicationRound.get("roundId"),
                                        ignored -> new java.util.LinkedHashSet<>()));
                    }
                    String toolCallId = firstText(payload, "toolCallId", "callId");
                    if (StringUtils.hasText(toolCallId)) {
                        Map<String, Object> tool = toolByCall.get(agent + "|" + toolCallId);
                        if (tool != null) {
                            addLifecycle(tool, "APPLIED_IN_ROUND");
                            tool.put("applicationRoundId", applicationRoundId);
                        }
                    }
                }
                case "skill.skipped" -> {
                    // A skipped skill never entered a model prompt. Audit only.
                }
                case "memory.used" -> {
                    String roundId = causalRoundId(payload, agent,
                            currentIterationRound.get(agent));
                    Map<String, Object> round = findRound(
                            agent, roundId, intOf(payload.get("callIndex")),
                            roundsById, roundsByCallIndex);
                    if (round != null) {
                        attachMemoryContext(
                                round, payload, event,
                                contextKeysByRound.computeIfAbsent(
                                        agent + "|" + round.get("roundId"),
                                        ignored -> new java.util.LinkedHashSet<>()));
                    }
                }
                case "memory.read", "memory.selected", "memory.written",
                     "memory.skipped", "memory.missed" -> {
                    // Retrieval/selection/write lifecycle is audit data. Only memory.used
                    // (or llm.context.attached.memoryRefs) is model-input causality.
                }
                default -> {
                    // progress/queued events do not create rounds
                }
            }
        }

        List<Map<String, Object>> executionTree = new ArrayList<>();
        int totalLlmCalls = 0;
        for (Map.Entry<String, Map<String, Object>> entry : nodes.entrySet()) {
            Map<String, Object> node = entry.getValue();
            // memory.used events carry consumerAgent=SpecialistAgent for
            // attribution, but SpecialistAgent is not an executable node.
            // Do not surface that synthetic attribution as a permanently
            // RUNNING agent in a completed trace.
            if ("SpecialistAgent".equals(entry.getKey())
                    && !node.containsKey("startedAt")
                    && !node.containsKey("endedAt")
                    && rounds.get(entry.getKey()).isEmpty()
                    && deterministicSteps.get(entry.getKey()).isEmpty()) {
                continue;
            }
            List<Map<String, Object>> agentRounds = rounds.get(entry.getKey());
            for (int i = 0; i < agentRounds.size(); i++) {
                agentRounds.get(i).put("roundNum", i + 1);
            }
            node.put("rounds", agentRounds);
            node.put("totalRounds", agentRounds.size());
            List<Map<String, Object>> agentDeterministic = deterministicSteps.get(entry.getKey());
            if (agentDeterministic != null && !agentDeterministic.isEmpty()) {
                node.put("deterministicSteps", agentDeterministic);
            }
            totalLlmCalls += intOf(node.get("llmCalls"));
            executionTree.add(node);
        }
        executionTree.sort((a, b) -> Integer.compare(
                intOf(a.get("phase")), intOf(b.get("phase"))));
        tree.put("executionTree", executionTree);
        tree.put("planReason", planReason);
        tree.put("parallelGroups", groups);
        tree.put("runId", run.getRunId());
        tree.put("runStatus", run.getStatus());
        tree.put("attemptNo", (run.getRetryCount() != null ? run.getRetryCount() : 0) + 1);

        List<String> planned = new ArrayList<>(nodes.keySet());
        Map<String, Object> route = new LinkedHashMap<>();
        route.put("routeMode", planReason.startsWith("rule_based")
                ? "规则直路由（简单请求零规划开销）" : "Coordinator LLM 动态规划");
        route.put("selectedAgents", planned);
        route.put("parallelGroups", groups);
        route.put("whySelected", planReason.isEmpty()
                ? List.of() : List.of(planReason));
        route.put("estimatedLlmCalls", totalLlmCalls);
        route.put("memoryHitCount", memoryHits);
        Map<String, Object> harnessPlan = new LinkedHashMap<>();
        harnessPlan.put("version", "unified-runtime-1");
        harnessPlan.put("route", route);
        harnessPlan.put("reportMode", "structured_evidence_report");
        Map<String, Object> memoryInfluence = new LinkedHashMap<>();
        memoryInfluence.put("hitCount", memoryHits);
        memoryInfluence.put("influences", memoryTop);
        harnessPlan.put("memoryInfluence", memoryInfluence);
        tree.put("harnessPlan", harnessPlan);
        tree.put("memoryHits", memoryHits);
        tree.put("memoryTop", memoryTop);

        tree.put("historicalAttempts", buildHistoricalAttempts(traceId, run.getRunId()));
        return tree;
    }

    private static Map<String, Object> roundFor(
            String agent, String roundId, Map<String, Object> payload, RunEvent event,
            Map<String, List<Map<String, Object>>> rounds,
            Map<String, Map<String, Map<String, Object>>> roundsById,
            Map<String, Map<Integer, Map<String, Object>>> roundsByCallIndex) {
        Map<String, Object> round = roundsById.get(agent).get(roundId);
        if (round == null) {
            round = new LinkedHashMap<>();
            round.put("roundId", roundId);
            round.put("type", "generation");
            round.put("title", "LLM 生成");
            round.put("category", "llm");
            round.put("contextEvents", new ArrayList<Map<String, Object>>());
            round.put("toolCalls", new ArrayList<Map<String, Object>>());
            round.put("firstEventSeq", event.getSeq());
            roundsById.get(agent).put(roundId, round);
            rounds.get(agent).add(round);
        }
        int callIndex = intOf(payload.get("callIndex"));
        if (callIndex > 0) {
            round.put("callIndex", callIndex);
            roundsByCallIndex.get(agent).put(callIndex, round);
        }
        String parentSpanId = firstText(payload, "parentSpanId");
        if (StringUtils.hasText(parentSpanId)) {
            round.put("parentSpanId", parentSpanId);
        }
        return round;
    }

    private static Map<String, Object> findRound(
            String agent, String roundId, int callIndex,
            Map<String, Map<String, Map<String, Object>>> roundsById,
            Map<String, Map<Integer, Map<String, Object>>> roundsByCallIndex) {
        if (StringUtils.hasText(roundId)) {
            Map<String, Object> round = roundsById.getOrDefault(
                    agent, Map.of()).get(roundId);
            if (round != null) {
                return round;
            }
        }
        return callIndex > 0
                ? roundsByCallIndex.getOrDefault(agent, Map.of()).get(callIndex)
                : null;
    }

    private void attachContextReferences(Map<String, Object> round,
                                         Map<String, Object> payload,
                                         RunEvent event,
                                         Set<String> seen) {
        Object memoryRefs = payload.get("memoryRefs");
        if (memoryRefs instanceof List<?> refs) {
            for (Object ref : refs) {
                Map<String, Object> memory = objectMap(ref);
                if (!memory.isEmpty()) {
                    attachMemoryContext(round, memory, event, seen);
                }
            }
        }
        Object skillRefs = payload.get("skillRefs");
        if (skillRefs instanceof List<?> refs) {
            for (Object ref : refs) {
                Map<String, Object> skill = objectMap(ref);
                if (skill.isEmpty() && ref != null) {
                    skill.put("skillId", String.valueOf(ref));
                }
                if (!skill.isEmpty()) {
                    attachSkillContext(round, skill, event, seen);
                }
            }
        }
        List<Map<String, Object>> catalogs = new ArrayList<>();
        Object toolCatalogRefs = payload.get("toolCatalogRefs");
        if (toolCatalogRefs instanceof List<?> refs) {
            int index = 0;
            for (Object ref : refs) {
                Map<String, Object> catalog = objectMap(ref);
                if (catalog.isEmpty()) {
                    continue;
                }
                String name = firstText(catalog, "name", "toolName", "modelName");
                String server = firstText(catalog, "mcpServer", "server");
                String source = firstText(catalog, "source", "kind", "origin");
                String category = "mcp".equalsIgnoreCase(source)
                        || StringUtils.hasText(server) ? "mcp" : "tool_catalog";
                Map<String, Object> context = new LinkedHashMap<>();
                context.put("eventId", "run-ev-" + event.getId() + "-catalog-" + index++);
                context.put("eventType", "tool.catalog.attached");
                context.put("category", category);
                context.put("title", ("mcp".equals(category)
                        ? "MCP 工具描述 · " : "工具描述 · ") + name);
                context.put("name", name);
                context.put("mcpServer", server);
                context.put("modelName", catalog.getOrDefault("modelName", name));
                context.put("source", source);
                context.put("status", "ATTACHED");
                context.put("occurredAt", eventInstant(payload, event));
                String key = "catalog|" + server + "|" + name;
                if (seen.add(key)) {
                    contextEvents(round).add(context);
                    catalogs.add(catalog);
                }
            }
        }
        if (!catalogs.isEmpty()) {
            round.put("toolCatalogRefs", catalogs);
        }
        // Results from the preceding tool round are inputs to this model turn,
        // not independent siblings in the execution tree.  The runtime emits
        // their call ids with the assembled prompt; retain that exact linkage
        // so the UI can render “LLM observed tool result” without guessing by
        // timestamps or display name.
        List<String> observedToolCallIds = stringList(payload.get("observedToolCallIds"));
        if (!observedToolCallIds.isEmpty()) {
            round.put("observedToolCallIds", observedToolCallIds);
        }
    }

    private static void attachMemoryContext(Map<String, Object> round,
                                            Map<String, Object> payload,
                                            RunEvent event,
                                            Set<String> seen) {
        String memoryId = firstText(payload, "memoryId", "id");
        String taxonomy = memoryTaxonomy(payload);
        String namespace = firstText(payload, "namespace", "source");
        String key = "memory|" + memoryId + "|" + taxonomy + "|" + namespace;
        if (!seen.add(key)) {
            return;
        }
        Map<String, Object> context = new LinkedHashMap<>();
        context.put("eventId", "run-ev-" + event.getId() + "-memory-" + memoryId);
        context.put("eventType", "memory.used");
        context.put("category", "memory");
        context.put("title", "记忆注入 · " + taxonomy);
        context.put("memoryId", memoryId);
        context.put("memoryType", taxonomy);
        context.put("taxonomy", taxonomy);
        context.put("namespace", StringUtils.hasText(namespace) ? namespace : "unknown");
        context.put("source", payload.getOrDefault("source", ""));
        context.put("reason", payload.getOrDefault("reason", "selected_for_agent_context"));
        context.put("status", "ATTACHED");
        if (payload.get("score") != null) {
            context.put("score", payload.get("score"));
        }
        context.put("occurredAt", eventInstant(payload, event));
        contextEvents(round).add(context);
    }

    private static void attachSkillContext(Map<String, Object> round,
                                           Map<String, Object> payload,
                                           RunEvent event,
                                           Set<String> seen) {
        String skillId = firstText(payload, "skillId", "id", "name");
        String version = firstText(payload, "skillVersion", "version");
        String key = "skill|" + skillId + "|" + version;
        if (!seen.add(key)) {
            return;
        }
        String stage = firstText(payload, "lifecycleStage", "stage");
        if (!StringUtils.hasText(stage)) {
            if (Boolean.TRUE.equals(payload.get("applied"))) {
                stage = "APPLIED";
            } else if (Boolean.TRUE.equals(payload.get("loaded"))) {
                stage = "LOADED";
            } else {
                stage = "SELECTED_METADATA";
            }
        }
        Map<String, Object> context = new LinkedHashMap<>();
        context.put("eventId", "run-ev-" + event.getId() + "-skill-" + skillId);
        context.put("eventType", "skill.applied");
        context.put("category", "skill");
        context.put("title", "Skill 上下文 · " + skillId);
        context.put("skillId", skillId);
        context.put("skillVersion", version);
        context.put("lifecycleStage", stage);
        context.put("disclosureState", payload.getOrDefault(
                "disclosureState", "APPLIED".equals(stage)
                        ? "INSTRUCTIONS" : "METADATA"));
        context.put("reason", payload.getOrDefault(
                "reason", payload.getOrDefault("triggerReason", "model_input")));
        context.put("status", "ATTACHED");
        context.put("occurredAt", eventInstant(payload, event));
        contextEvents(round).add(context);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> contextEvents(Map<String, Object> round) {
        return (List<Map<String, Object>>) round.computeIfAbsent(
                "contextEvents", ignored -> new ArrayList<Map<String, Object>>());
    }

    private Map<String, Object> newTool(RunEvent event,
                                        Map<String, Object> payload,
                                        String toolCallId) {
        Map<String, Object> tool = new LinkedHashMap<>();
        String toolName = StringUtils.hasText(event.getToolName())
                ? event.getToolName() : firstText(payload, "toolName", "modelName");
        String kind = firstText(payload, "kind", "origin", "source");
        if (!StringUtils.hasText(kind)) {
            kind = inferKind(toolName, event.getEventType());
        }
        if ("internal".equalsIgnoreCase(kind)
                && ("load_skill".equals(toolName) || "read_skill_resource".equals(toolName))) {
            kind = "skill";
        }
        tool.put("toolCallId", toolCallId);
        tool.put("name", toolName);
        tool.put("category", kind);
        tool.put("origin", payload.getOrDefault("origin", kind));
        tool.put("lifecycle", new ArrayList<String>());
        tool.put("eventIds", new ArrayList<String>());
        return tool;
    }

    private void mergeToolEvent(Map<String, Object> tool,
                                RunEvent event,
                                Map<String, Object> payload,
                                Object startedArguments,
                                Map<String, Object> indexedProposal) {
        @SuppressWarnings("unchecked")
        List<String> eventIds = (List<String>) tool.get("eventIds");
        eventIds.add("run-ev-" + event.getId());
        copyIfPresent(tool, payload,
                "mcpServer", "protocolVersion", "skillId", "skillVersion",
                "executionBackend", "lifecycleStage");
        String type = event.getEventType();
        String stage = stringOf(payload.get("lifecycleStage"));
        if (StringUtils.hasText(stage)) {
            addLifecycle(tool, stage);
        }
        if ("tool.progress".equals(type) && "LLM_PROPOSED".equals(stage)) {
            tool.put("proposalSource", "LLM_NATIVE");
            tool.put("proposalOccurredAt", eventInstant(payload, event));
            tool.put("modelToolName", payload.getOrDefault(
                    "modelName", event.getToolName()));
            if (payload.get("arguments") != null) {
                tool.put("modelGeneratedArguments", preview(payload.get("arguments"), 800));
                tool.put("input", preview(payload.get("arguments"), 800));
            }
        } else if ("tool.started".equals(type)) {
            tool.put("status", "RUNNING");
            tool.put("startedAt", eventStart(payload, event));
            tool.putIfAbsent("occurredAt", eventStart(payload, event));
            Object arguments = payload.get("arguments") != null
                    ? payload.get("arguments") : startedArguments;
            if (arguments != null) {
                tool.put("input", preview(arguments, 800));
            }
        } else if ("tool.completed".equals(type) || "tool.failed".equals(type)) {
            tool.put("status", "tool.completed".equals(type) ? "SUCCESS" : "FAILED");
            tool.put("durationMs", longOf(payload.get("durationMs")));
            tool.put("endedAt", eventEnd(payload, event));
            tool.putIfAbsent("occurredAt", eventInstant(payload, event));
            Object result = payload.getOrDefault(
                    "resultPreview", payload.getOrDefault("error", ""));
            tool.put("result", preview(result, 800));
            tool.put("output", preview(result, 1600));
            Object arguments = payload.get("arguments") != null
                    ? payload.get("arguments") : startedArguments;
            if (arguments != null) {
                tool.put("input", preview(arguments, 800));
            }
        }
        if (indexedProposal != null && tool.get("proposalSource") == null) {
            tool.put("proposalSource", "LLM_NATIVE");
            tool.put("modelGeneratedArguments",
                    preview(indexedProposal.get("arguments"), 800));
            tool.put("proposalOccurredAt", temporalValue(
                    indexedProposal, "occurredAt", "startedAt", "endedAt",
                    event.getCreateTime()));
            tool.put("modelToolName", indexedProposal.getOrDefault(
                    "modelName", event.getToolName()));
        }
    }

    private static void mergeSkillLifecycle(Map<String, Object> tool,
                                            RunEvent event,
                                            Map<String, Object> payload) {
        tool.put("category", "skill");
        tool.put("origin", "skill_manager");
        copyIfPresent(tool, payload,
                "skillId", "skillVersion", "skillHash",
                "lifecycleStage", "disclosureState", "resourcePath");
        String stage = firstText(payload, "lifecycleStage");
        addLifecycle(tool, StringUtils.hasText(stage) ? stage
                : "skill.failed".equals(event.getEventType()) ? "ERROR" : "LOADED");
        tool.put("status", "skill.failed".equals(event.getEventType())
                ? "FAILED" : "SUCCESS");
        tool.put("result", "stage=" + payload.getOrDefault(
                "lifecycleStage", event.getEventType())
                + " reason=" + payload.getOrDefault(
                        "reason", payload.getOrDefault("triggerReason", "llm_requested")));
        tool.putIfAbsent("occurredAt", eventInstant(payload, event));
    }

    @SuppressWarnings("unchecked")
    private static void addLifecycle(Map<String, Object> tool, String stage) {
        if (!StringUtils.hasText(stage)) {
            return;
        }
        List<String> lifecycle = (List<String>) tool.computeIfAbsent(
                "lifecycle", ignored -> new ArrayList<String>());
        if (!lifecycle.contains(stage)) {
            lifecycle.add(stage);
        }
    }

    @SuppressWarnings("unchecked")
    private static void attachTool(Map<String, Object> round,
                                   Map<String, Object> tool) {
        List<Map<String, Object>> tools = (List<Map<String, Object>>) round.computeIfAbsent(
                "toolCalls", ignored -> new ArrayList<Map<String, Object>>());
        if (!tools.contains(tool)) {
            tools.add(tool);
        }
        round.put("hasToolCalls", true);
    }

    private static void attachDeterministicStep(List<Map<String, Object>> steps,
                                                Map<String, Object> tool) {
        if (!steps.contains(tool)) {
            steps.add(tool);
        }
    }

    private static String causalRoundId(Map<String, Object> payload,
                                        String agent,
                                        String fallback) {
        String roundId = firstText(payload,
                "roundId", "applicationRoundId", "proposalRoundId",
                "parentRoundId", "parentSpanId", "llmRoundId");
        if (StringUtils.hasText(roundId)) {
            return roundId;
        }
        int iteration = intOf(payload.get("iteration"));
        if (iteration > 0) {
            return agent + "-iteration-" + iteration;
        }
        return fallback;
    }

    private static String causalParentSpan(String eventType,
                                           Map<String, Object> payload) {
        if ("skill.applied".equals(eventType)) {
            return firstText(payload,
                    "applicationRoundId", "parentRoundId", "parentSpanId", "roundId");
        }
        if ("skill.loaded".equals(eventType) || "skill.failed".equals(eventType)) {
            return firstText(payload,
                    "proposalRoundId", "parentRoundId", "parentSpanId", "roundId");
        }
        if (eventType.startsWith("tool.") || eventType.startsWith("memory.")
                || "llm.context.attached".equals(eventType)) {
            return firstText(payload,
                    "roundId", "parentRoundId", "parentSpanId", "llmRoundId");
        }
        return firstText(payload, "parentSpanId", "parentRoundId");
    }

    private static String firstText(Map<String, Object> payload, String... keys) {
        for (String key : keys) {
            String value = stringOf(payload.get(key));
            if (StringUtils.hasText(value)) {
                return value;
            }
        }
        return "";
    }

    private static void copyIfPresent(Map<String, Object> target,
                                      Map<String, Object> source,
                                      String... keys) {
        for (String key : keys) {
            if (source.get(key) != null) {
                target.put(key, source.get(key));
            }
        }
    }

    private static List<String> stringList(Object value) {
        List<String> out = new ArrayList<>();
        if (value instanceof List<?> list) {
            for (Object item : list) {
                if (item != null) {
                    out.add(String.valueOf(item));
                }
            }
        }
        return out;
    }

    private static Map<String, Object> objectMap(Object value) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (value instanceof Map<?, ?> raw) {
            for (Map.Entry<?, ?> entry : raw.entrySet()) {
                if (entry.getKey() != null) {
                    out.put(String.valueOf(entry.getKey()), entry.getValue());
                }
            }
        }
        return out;
    }

    private static Object eventInstant(Map<String, Object> payload, RunEvent event) {
        Object value = firstValue(payload, "occurredAt", "startedAt", "endedAt");
        return value != null ? value : event.getCreateTime();
    }

    private static Object eventStart(Map<String, Object> payload, RunEvent event) {
        Object value = firstValue(payload, "startedAt", "startAt", "occurredAt");
        return value != null ? value : event.getCreateTime();
    }

    private static Object eventEnd(Map<String, Object> payload, RunEvent event) {
        Object value = firstValue(payload, "endedAt", "endAt", "occurredAt");
        return value != null ? value : event.getCreateTime();
    }

    private static Object firstValue(Map<String, Object> payload, String... keys) {
        for (String key : keys) {
            Object value = payload.get(key);
            if (value != null && StringUtils.hasText(String.valueOf(value))) {
                return value;
            }
        }
        return null;
    }

    private List<Map<String, Object>> buildHistoricalAttempts(String traceId, String currentRunId) {
        List<Map<String, Object>> attempts = new ArrayList<>();
        for (AgentRun prior : listAttemptRuns(traceId, currentRunId)) {
            String errorCode = prior.getErrorCode() != null ? prior.getErrorCode() : "";
            boolean controlPlane = CONTROL_PLANE_ERRORS.contains(errorCode)
                    || "CONTROL_PLANE".equalsIgnoreCase(errorCode);
            // Prefer surfacing control-plane failures; also keep other failed attempts.
            if (!controlPlane && !isFailedAttempt(prior)) {
                continue;
            }
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("runId", prior.getRunId());
            row.put("status", prior.getStatus());
            row.put("errorCode", errorCode);
            row.put("errorMessage", prior.getErrorMessage());
            row.put("attemptNo", (prior.getRetryCount() != null ? prior.getRetryCount() : 0) + 1);
            row.put("category", "CONTROL_PLANE");
            row.put("retryable", CONTROL_PLANE_ERRORS.contains(errorCode));
            row.put("controlPlaneStage", stageForError(errorCode));
            row.put("finishedAt", prior.getFinishedAt() != null
                    ? String.valueOf(prior.getFinishedAt()) : null);
            row.put("createdAt", prior.getCreatedAt() != null
                    ? String.valueOf(prior.getCreatedAt()) : null);
            attempts.add(row);
        }
        return attempts;
    }

    private static boolean isFailedAttempt(AgentRun run) {
        String status = run.getStatus();
        return "FAILED".equals(status) || "TIMED_OUT".equals(status) || "CANCELLED".equals(status);
    }

    private static String stageForError(String errorCode) {
        if (errorCode == null || errorCode.isBlank()) {
            return "unknown";
        }
        return switch (errorCode) {
            case "RUNTIME_START_FAILED", "START_STUCK" -> "start";
            case "ORPHANED_ON_RESTART" -> "restart_recovery";
            default -> "control_plane";
        };
    }

    private static String memoryTaxonomy(Map<String, Object> payload) {
        String supplied = stringOf(payload.get("taxonomy"));
        if (!StringUtils.hasText(supplied)) {
            supplied = stringOf(payload.get("memoryType"));
        }
        if (!StringUtils.hasText(supplied)) {
            supplied = stringOf(payload.get("type"));
        }
        if ("MULTI".equalsIgnoreCase(supplied)) {
            return "MULTI";
        }
        String canonical = MemoryService.canonicalTaxonomy(supplied);
        return MemoryService.TYPES.contains(canonical) ? canonical : "UNKNOWN";
    }

    private static String memoryRoundTitle(String eventType, String taxonomy) {
        String action = switch (eventType) {
            case "memory.read" -> "检索";
            case "memory.selected" -> "命中";
            case "memory.used" -> "使用";
            case "memory.written" -> "写入";
            case "memory.skipped" -> "跳过";
            default -> "未命中";
        };
        return "记忆 " + action + " · " + taxonomy;
    }

    private static String skillLifecycleTitle(String eventType, String skillId, String version) {
        String stage = switch (eventType) {
            case "skill.loaded" -> "渐进加载";
            case "skill.applied" -> "已应用";
            case "skill.skipped" -> "已跳过";
            case "skill.failed" -> "失败";
            default -> "生命周期";
        };
        return "[Skill " + stage + "] " + skillId + "@" + version;
    }

    private static LocalDateTime eventOccurredAt(Map<String, Object> payload,
                                                  LocalDateTime fallback) {
        String raw = stringOf(payload.get("occurredAt"));
        if (StringUtils.hasText(raw)) {
            try {
                return LocalDateTime.ofInstant(Instant.parse(raw), ZoneOffset.UTC);
            } catch (Exception ignored) {
                try {
                    return LocalDateTime.ofInstant(
                            OffsetDateTime.parse(raw).toInstant(), ZoneOffset.UTC);
                } catch (Exception ignoredOffset) {
                    // Use durable event createTime below.
                }
            }
        }
        return fallback != null ? fallback : LocalDateTime.now(ZoneOffset.UTC);
    }

    private static Object temporalValue(Map<String, Object> payload,
                                        String primary, String secondary,
                                        String tertiary, LocalDateTime fallback) {
        for (String key : List.of(primary, secondary, tertiary)) {
            Object value = payload.get(key);
            if (value != null && StringUtils.hasText(String.valueOf(value))) {
                return value;
            }
        }
        return fallback != null ? fallback : LocalDateTime.now(ZoneOffset.UTC);
    }

    private static void putTemporalFields(Map<String, Object> target,
                                          Map<String, Object> payload,
                                          RunEvent event) {
        Object occurredAt = temporalValue(payload, "occurredAt", "startedAt",
                "endedAt", event.getCreateTime());
        target.put("occurredAt", occurredAt);
        target.put("startedAt", temporalValue(payload, "startedAt", "startAt",
                "occurredAt", event.getCreateTime()));
        target.put("endedAt", temporalValue(payload, "endedAt", "endAt",
                "occurredAt", event.getCreateTime()));
    }

    private static List<Object> canonicalMemoryRows(List<?> rows, RunEvent event) {
        List<Object> out = new ArrayList<>();
        for (Object item : rows) {
            if (!(item instanceof Map<?, ?> raw)) {
                continue;
            }
            Map<String, Object> row = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : raw.entrySet()) {
                if (entry.getKey() != null) {
                    row.put(String.valueOf(entry.getKey()), entry.getValue());
                }
            }
            String taxonomy = memoryTaxonomy(row);
            row.put("type", taxonomy);
            row.put("memoryType", taxonomy);
            row.put("taxonomy", taxonomy);
            row.putIfAbsent("occurredAt",
                    event.getCreateTime() != null
                            ? String.valueOf(event.getCreateTime())
                            : Instant.now().toString());
            // Never carry memory正文 into the observability projection.
            row.remove("content");
            row.remove("structuredContent");
            out.add(row);
        }
        return out;
    }

    private static String inferKind(String toolName, String eventType) {
        if (eventType != null && eventType.startsWith("skill.")) {
            return "skill";
        }
        if (toolName == null) {
            return "builtin";
        }
        if (toolName.startsWith("retrieval.")) {
            return "retrieval";
        }
        if (toolName.startsWith("mcp_") || toolName.contains(".")) {
            return "mcp";
        }
        if ("execute_skill".equals(toolName) || "list_skills".equals(toolName)
                || "load_skill".equals(toolName)) {
            return "skill";
        }
        if (Set.of("parse_resume", "check_timeline", "calculate_jd_coverage",
                "locate_evidence", "resume_lint",
                "validate_report_schema").contains(toolName)) {
            return "builtin";
        }
        if ("external_profile_lookup".equals(toolName)) {
            return "external";
        }
        return "builtin";
    }

    // ------------------------------------------------------------------

    private static String runTitle(String type) {
        return switch (type) {
            case "run.queued" -> "已入队";
            case "run.started" -> "运行开始";
            case "run.progress" -> "运行进度";
            case "run.cancelling" -> "取消中";
            case "run.cancelled" -> "已取消";
            case "run.completed" -> "运行完成";
            case "run.failed" -> "运行失败";
            case "run.timed_out" -> "运行超时";
            default -> type;
        };
    }

    private static String mapEventKind(String type) {
        if (type.startsWith("llm.")) {
            return "LLM_GENERATION";
        }
        if (type.startsWith("skill.")) {
            return "SKILL_LIFECYCLE";
        }
        if (type.startsWith("memory.")) {
            return "MEMORY_LIFECYCLE";
        }
        if (type.startsWith("tool.")) {
            return "LLM_TOOL_CALL";
        }
        if (type.startsWith("agent.")) {
            return "AGENT_EXECUTION";
        }
        return "RUN_STATUS";
    }

    private static String agentless(String type) {
        return type.startsWith("run.") ? "Run" : "Runtime";
    }

    @SuppressWarnings("unchecked")
    private List<List<String>> castGroups(Object raw) {
        List<List<String>> groups = new ArrayList<>();
        if (raw instanceof List<?> outer) {
            for (Object inner : outer) {
                if (inner instanceof List<?> list) {
                    List<String> group = new ArrayList<>();
                    for (Object item : list) {
                        group.add(String.valueOf(item));
                    }
                    groups.add(group);
                }
            }
        }
        return groups;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readPayload(String json) {
        try {
            return json != null ? objectMapper.readValue(json, Map.class) : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    private String preview(Object value, int limit) {
        try {
            String text = value instanceof String s
                    ? s : objectMapper.writeValueAsString(value != null ? value : "");
            return text.length() > limit ? text.substring(0, limit) + "..." : text;
        } catch (Exception e) {
            return String.valueOf(value);
        }
    }

    private static String stringOf(Object value) {
        if (value == null) {
            return "";
        }
        String text = String.valueOf(value).trim();
        return "null".equalsIgnoreCase(text) ? "" : text;
    }

    private static Long longOf(Object value) {
        if (value instanceof Number number) {
            return number.longValue();
        }
        try {
            return value != null ? Long.parseLong(String.valueOf(value)) : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static int intOf(Object value) {
        if (value instanceof Number number) {
            return number.intValue();
        }
        try {
            return value != null ? Integer.parseInt(String.valueOf(value)) : 0;
        } catch (NumberFormatException e) {
            return 0;
        }
    }
}
