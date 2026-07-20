package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.TraceEventResponse;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.RunEvent;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
            case "sandbox.started", "sandbox.completed", "sandbox.failed" -> {
                title = "Sandbox " + event.getToolName();
                detail = preview(payload, 300);
                status = type.endsWith("failed") ? "FAILED" : "SUCCESS";
            }
            case "context.compacted" -> {
                title = "上下文压缩";
                detail = "tokens " + payload.getOrDefault("beforeTokens", "?")
                        + " → " + payload.getOrDefault("afterTokens", "?");
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
        return new TraceEventResponse(
                traceId,
                "run-ev-" + event.getId(),
                null,
                agent,
                mapEventKind(type),
                title,
                detail,
                status,
                duration,
                tokens,
                event.getCreateTime() != null ? event.getCreateTime() : LocalDateTime.now());
    }

    // ------------------------------------------------------------------
    // Phased execution tree (GET /api/tasks/{traceId}/agent-execution fallback)
    // ------------------------------------------------------------------

    public Map<String, Object> executionTreeForTrace(String traceId) {
        AgentRun run = findRunForTrace(traceId);
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("traceId", traceId);
        tree.put("framework", "Unified Agent Runtime + DeepSeek + Sandbox Tools");
        tree.put("architecture", "Coordinator 动态规划 + 并行 Specialist");
        if (run == null) {
            tree.put("executionTree", List.of());
            return tree;
        }
        List<RunEvent> events = eventService.listSince(run.getRunId(), 0, 2000);

        // phase = position of the agent's parallel group in the coordinator plan
        Map<String, Integer> phaseOf = new LinkedHashMap<>();
        List<List<String>> groups = List.of();
        String planReason = "";
        for (RunEvent event : events) {
            if ("agent.selected".equals(event.getEventType())) {
                Map<String, Object> payload = readPayload(event.getPayload());
                groups = castGroups(payload.get("parallelGroups"));
                planReason = String.valueOf(payload.getOrDefault("reason", ""));
                for (int i = 0; i < groups.size(); i++) {
                    for (String agent : groups.get(i)) {
                        phaseOf.put(agent, i + 1);
                    }
                }
            }
        }

        Map<String, Map<String, Object>> nodes = new LinkedHashMap<>();
        Map<String, List<Map<String, Object>>> rounds = new LinkedHashMap<>();
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
            }
            switch (event.getEventType()) {
                case "agent.started" -> {
                    node.put("role", payload.getOrDefault("description", ""));
                    node.put("startedAt", event.getCreateTime());
                }
                case "agent.completed" -> {
                    node.put("status", "SUCCESS");
                    node.put("durationMs", longOf(payload.get("durationMs")));
                    node.put("output", payload.getOrDefault("summary", ""));
                    node.put("endedAt", event.getCreateTime());
                    node.put("llmCalls", payload.getOrDefault("llmCalls", 0));
                    node.put("toolCalls", payload.getOrDefault("toolCalls", 0));
                    node.put("confidence", payload.getOrDefault("confidence", null));
                }
                case "agent.failed" -> {
                    node.put("status", "FAILED");
                    node.put("durationMs", longOf(payload.get("durationMs")));
                    node.put("output", payload.getOrDefault("error", ""));
                }
                case "llm.completed" -> {
                    int prompt = intOf(payload.get("promptTokens"));
                    int completion = intOf(payload.get("completionTokens"));
                    Map<String, Object> round = new LinkedHashMap<>();
                    round.put("roundNum", rounds.get(agent).size() + 1);
                    round.put("type", "generation");
                    round.put("title", "LLM 生成");
                    round.put("tokens", prompt + completion);
                    round.put("durationMs", longOf(payload.get("durationMs")));
                    round.put("model", payload.getOrDefault("model", ""));
                    rounds.get(agent).add(round);
                }
                case "llm.retrying", "llm.failed" -> {
                    Map<String, Object> round = new LinkedHashMap<>();
                    round.put("roundNum", rounds.get(agent).size() + 1);
                    round.put("type", "generation");
                    round.put("title", "llm.failed".equals(event.getEventType())
                            ? "LLM 失败" : "LLM 重试");
                    round.put("error", payload.getOrDefault("error", ""));
                    rounds.get(agent).add(round);
                }
                case "tool.completed", "tool.failed" -> {
                    Map<String, Object> round = new LinkedHashMap<>();
                    round.put("roundNum", rounds.get(agent).size() + 1);
                    round.put("type", "tool");
                    round.put("title", "工具 " + event.getToolName());
                    round.put("hasToolCalls", true);
                    Map<String, Object> tool = new LinkedHashMap<>();
                    tool.put("toolCallId", payload.getOrDefault("toolCallId", ""));
                    tool.put("name", event.getToolName());
                    tool.put("category", "tool");
                    tool.put("status", "tool.completed".equals(event.getEventType())
                            ? "SUCCESS" : "FAILED");
                    tool.put("durationMs", longOf(payload.get("durationMs")));
                    tool.put("result", preview(payload.getOrDefault(
                            "resultPreview", payload.getOrDefault("error", "")), 800));
                    round.put("toolCalls", List.of(tool));
                    rounds.get(agent).add(round);
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
            List<Map<String, Object>> agentRounds = rounds.get(entry.getKey());
            node.put("rounds", agentRounds);
            node.put("totalRounds", agentRounds.size());
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
        tree.put("policyId", run.getPolicyId());
        // The "动态路由决策" card reads this structure (route decision view).
        List<String> planned = new ArrayList<>(nodes.keySet());
        Map<String, Object> route = new LinkedHashMap<>();
        route.put("routeMode", planReason.startsWith("rule_based")
                ? "规则直路由（简单请求零规划开销）" : "Coordinator LLM 动态规划");
        route.put("selectedAgents", planned);
        route.put("parallelGroups", groups);
        route.put("whySelected", planReason.isEmpty()
                ? List.of() : List.of(planReason));
        route.put("estimatedLlmCalls", totalLlmCalls);
        Map<String, Object> harnessPlan = new LinkedHashMap<>();
        harnessPlan.put("version", "unified-runtime-1");
        harnessPlan.put("route", route);
        harnessPlan.put("reportMode", "structured_evidence_report");
        tree.put("harnessPlan", harnessPlan);
        return tree;
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
        if (type.startsWith("tool.") || type.startsWith("sandbox.")) {
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
