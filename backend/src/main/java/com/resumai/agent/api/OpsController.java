package com.resumai.agent.api;

import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugDetailResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugSummary;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillOpsResponse;
import com.resumai.agent.service.ops.OpsDebugService;
import com.resumai.agent.service.run.AgentRuntimeClient;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Agent Ops = run-centric Debug Console.
 * MCP/Skills status comes from the Python runtime registry, not config text inference.
 * Typed drilldown also lives under /api/dev/* and shares {@link OpsDebugService}.
 */
@RestController
@RequestMapping("/api/ops")
public class OpsController {

    private final OpsDebugService opsDebugService;
    private final AgentRuntimeClient runtimeClient;

    public OpsController(OpsDebugService opsDebugService,
                         AgentRuntimeClient runtimeClient) {
        this.opsDebugService = opsDebugService;
        this.runtimeClient = runtimeClient;
    }

    /** Lightweight shell — tab panels load their own paginated endpoints. */
    @GetMapping
    public Map<String, Object> overview() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("panels", List.of("runs", "memory", "mcp", "skills", "rag"));
        body.put("role", "developer_console");
        body.put("description", "Run-centric Debug Console：按 run 下钻 MCP/Skills/Memory/RAG");
        body.put("runtimeReady", runtimeClient.isReady());
        body.put("recentRuns", runs(null, null, null, null, 20).get("items"));
        return body;
    }

    @GetMapping("/runs")
    public Map<String, Object> runs(@RequestParam(required = false) String traceId,
                                    @RequestParam(required = false) String runId,
                                    @RequestParam(required = false) String conversationId,
                                    @RequestParam(required = false) String status,
                                    @RequestParam(defaultValue = "40") int limit) {
        List<RunDebugSummary> items = opsDebugService.listRuns(
                traceId, runId, conversationId, status, limit);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", items.size());
        body.put("items", items);
        return body;
    }

    @GetMapping("/runs/{runId}")
    public Map<String, Object> runDetail(@PathVariable String runId,
                                         @RequestParam(defaultValue = "80") int eventLimit) {
        RunDebugDetailResponse detail = opsDebugService.runDetail(runId, eventLimit, null);
        if (detail == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("run", detail.run());
        body.put("correlation", detail.correlation());
        body.put("plan", detail.plan());
        body.put("budget", detail.budget());
        body.put("artifacts", detail.artifacts());
        body.put("skillVersions", detail.run().skillVersions());
        body.put("promptVersions", detail.run().promptVersions());
        body.put("metrics", detail.run().metrics());
        body.put("skillsSelected", detail.skills());
        body.put("skills", detail.skills());
        body.put("mcpCalls", detail.mcpCalls());
        body.put("errors", detail.errors());
        body.put("memory", detail.memory());
        body.put("timeline", detail.timeline());
        body.put("eventCount", detail.timeline().size());
        body.put("events", detail.timeline());
        body.put("truncated", detail.truncated());
        body.put("nextSeq", detail.nextSeq());
        return body;
    }

    @GetMapping("/memory")
    public Map<String, Object> memory(@RequestParam(defaultValue = "50") int limit,
                                      @RequestParam(required = false) String scope,
                                      @RequestParam(required = false) String source,
                                      @RequestParam(required = false) String runId,
                                      @RequestParam(required = false) String decision,
                                      @RequestParam(defaultValue = "false") boolean includeBenchmark,
                                      @RequestParam(defaultValue = "false") boolean includeControlFailure) {
        MemoryOpsResponse resp = opsDebugService.memory(
                limit, scope, source, runId, decision, includeBenchmark, includeControlFailure);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", resp.count());
        body.put("skipped", resp.skipped());
        body.put("byType", resp.byType());
        body.put("byScope", resp.byScope());
        body.put("bySource", resp.bySource());
        body.put("entries", resp.entries());
        body.put("usage", resp.usage());
        body.put("preferences", resp.entries().stream()
                .filter(e -> "PREFERENCE".equalsIgnoreCase(String.valueOf(e.get("type"))))
                .toList());
        body.put("summaries", resp.entries().stream()
                .filter(e -> {
                    String t = String.valueOf(e.get("type"));
                    return "CONVERSATION".equalsIgnoreCase(t) || "EPISODIC".equalsIgnoreCase(t);
                })
                .toList());
        body.put("hits", resp.entries().stream()
                .filter(e -> {
                    String t = String.valueOf(e.get("type"));
                    return !"PREFERENCE".equalsIgnoreCase(t)
                            && !"CONVERSATION".equalsIgnoreCase(t)
                            && !"EPISODIC".equalsIgnoreCase(t);
                })
                .toList());
        body.put("defaults", resp.defaults());
        body.put("fileStore", resp.fileStore());
        return body;
    }

    @GetMapping("/mcp")
    public Map<String, Object> mcp(@RequestParam(defaultValue = "false") boolean probe,
                                   @RequestParam(defaultValue = "40") int recentLimit,
                                   @RequestParam(required = false) String runId,
                                   @RequestParam(required = false) String server,
                                   @RequestParam(required = false) String outcome) {
        McpOpsResponse resp = opsDebugService.mcp(probe, runId, server, outcome, recentLimit);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("statusEnum", resp.statusEnum());
        body.put("source", resp.inventory().source());
        body.put("probed", resp.inventory().probed());
        body.put("lastProbeAt", resp.inventory().lastProbeAt());
        body.put("availableTools", resp.inventory().availableTools());
        body.put("toolCount", resp.inventory().toolCount());
        body.put("configPath", resp.inventory().configPath());
        body.put("servers", resp.inventory().servers());
        body.put("runtimeReachable", resp.inventory().runtimeReachable());
        if (resp.inventory().runtimeError() != null) {
            body.put("runtimeError", resp.inventory().runtimeError());
        }
        body.put("inventory", resp.inventory());
        body.put("invocations", resp.invocations());
        body.put("recentCalls", resp.invocations().items());
        body.put("endpointStats", resp.endpointStats());
        body.put("note", resp.note());
        return body;
    }

    @GetMapping("/skills")
    public Map<String, Object> skills(@RequestParam(defaultValue = "false") boolean includeDeprecated,
                                      @RequestParam(defaultValue = "60") int recentLimit) {
        SkillOpsResponse resp = opsDebugService.skills(includeDeprecated, recentLimit);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("source", resp.source());
        body.put("root", resp.root());
        body.put("count", resp.count());
        body.put("activeCount", resp.activeCount());
        body.put("deprecatedCount", resp.deprecatedCount());
        body.put("advertisedTools", resp.advertisedTools());
        body.put("skills", resp.skills());
        body.put("runtimeReachable", resp.runtimeReachable());
        if (resp.runtimeError() != null) {
            body.put("runtimeError", resp.runtimeError());
        }
        body.put("selectedApplied", resp.selectedApplied());
        body.put("usageBySkill", resp.usageBySkill());
        body.put("note", resp.note());
        return body;
    }

    @GetMapping("/rag")
    public RagOpsResponse rag(@RequestParam(defaultValue = "100") int limit,
                              @RequestParam(required = false) String runId,
                              @RequestParam(required = false) String agentId,
                              @RequestParam(required = false) String outcome) {
        return opsDebugService.rag(limit, runId, agentId, outcome);
    }

}
