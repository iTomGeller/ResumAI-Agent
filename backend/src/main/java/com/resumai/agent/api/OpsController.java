package com.resumai.agent.api;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagOpsResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugDetailResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugSummary;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillOpsResponse;
import com.resumai.agent.config.LangfuseHealthService;
import com.resumai.agent.dao.PolicyRewardMapper;
import com.resumai.agent.dao.SandboxExecutionMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicyRewardRow;
import com.resumai.agent.domain.entity.PolicyStatisticsRow;
import com.resumai.agent.domain.entity.SandboxExecutionRow;
import com.resumai.agent.service.ops.OpsDebugService;
import com.resumai.agent.service.run.AgentRuntimeClient;
import com.resumai.agent.service.run.PolicyService;
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
 * Sandbox surfaces are Policy Lab only — never candidate evaluation.
 * Typed drilldown also lives under /api/dev/* and shares {@link OpsDebugService}.
 */
@RestController
@RequestMapping("/api/ops")
public class OpsController {

    private final OpsDebugService opsDebugService;
    private final SandboxExecutionMapper sandboxExecutionMapper;
    private final PolicyService policyService;
    private final PolicyRewardMapper policyRewardMapper;
    private final AgentRuntimeClient runtimeClient;
    private final LangfuseHealthService langfuseHealth;

    public OpsController(OpsDebugService opsDebugService,
                         SandboxExecutionMapper sandboxExecutionMapper,
                         PolicyService policyService,
                         PolicyRewardMapper policyRewardMapper,
                         AgentRuntimeClient runtimeClient,
                         LangfuseHealthService langfuseHealth) {
        this.opsDebugService = opsDebugService;
        this.sandboxExecutionMapper = sandboxExecutionMapper;
        this.policyService = policyService;
        this.policyRewardMapper = policyRewardMapper;
        this.runtimeClient = runtimeClient;
        this.langfuseHealth = langfuseHealth;
    }

    /** Lightweight shell — tab panels load their own paginated endpoints. */
    @GetMapping
    public Map<String, Object> overview() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("panels", List.of(
                "runs", "memory", "policyLab", "mcp", "skills", "rag", "observability"));
        body.put("role", "developer_console");
        body.put("description", "Run-centric Debug Console：按 run 下钻 MCP/Skills/Memory；Sandbox 仅属 Policy Lab");
        body.put("runtimeReady", runtimeClient.isReady());
        body.put("recentRuns", runs(null, null, null, null, 20).get("items"));
        body.put("observability", Map.of("langfuse", langfuseHealth.snapshot()));
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
        body.put("observability", detail.observability());
        body.put("timeline", detail.timeline());
        body.put("eventCount", detail.timeline().size());
        body.put("events", detail.timeline());
        body.put("truncated", detail.truncated());
        body.put("nextSeq", detail.nextSeq());
        return body;
    }

    @GetMapping("/observability")
    public Map<String, Object> observability() {
        langfuseHealth.refreshProbe();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("langfuse", langfuseHealth.snapshot());
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

    /**
     * Policy Lab sandbox executions — purpose taken from row when present.
     */
    @GetMapping({"/sandbox", "/policy-lab"})
    public Map<String, Object> policyLabSandbox(@RequestParam(defaultValue = "50") int limit) {
        int cap = Math.max(1, Math.min(limit, 200));
        List<SandboxExecutionRow> rows = sandboxExecutionMapper.selectList(
                new QueryWrapper<SandboxExecutionRow>().orderByDesc("create_time").last("limit " + cap));
        List<Map<String, Object>> executions = rows.stream().map(row -> {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("sandboxId", row.getSandboxId());
            item.put("runId", row.getRunId());
            item.put("conversationId", row.getConversationId());
            item.put("toolName", row.getToolName());
            item.put("containerId", row.getContainerId());
            item.put("status", row.getStatus());
            item.put("exitCode", row.getExitCode());
            item.put("durationMs", row.getDurationMs());
            item.put("error", row.getError());
            item.put("isolationMode", isolationMode(row));
            item.put("purpose", opsDebugService.sandboxPurpose(row.getPurpose()));
            item.put("experimentId", row.getExperimentId());
            item.put("trialId", row.getTrialId());
            item.put("createTime", row.getCreateTime());
            item.put("finishedAt", row.getFinishedAt());
            item.put("stdoutTail", truncate(row.getStdoutTail(), 240));
            item.put("stderrTail", truncate(row.getStderrTail(), 240));
            return item;
        }).toList();
        Map<String, Long> byStatus = new LinkedHashMap<>();
        for (Map<String, Object> exec : executions) {
            byStatus.merge(String.valueOf(exec.getOrDefault("status", "UNKNOWN")), 1L, Long::sum);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("label", "Policy Optimization Lab（无 GPU）");
        body.put("purpose", "SANDBOX");
        body.put("axes", Map.of(
                "ONLINE_SELECTION", "champion-only production / shadow bandit",
                "OFFLINE_SEARCH", "reflective evolutionary search (bounded, not full GEPA)",
                "MODEL_WEIGHTS", "unchanged",
                "SANDBOX", "experiment isolation"));
        body.put("candidateEvaluation", false);
        body.put("disclaimer", "SANDBOX 仅服务 Policy Optimization Lab / benchmark / replay，不属于候选人评估路径。");
        body.put("count", executions.size());
        body.put("byStatus", byStatus);
        body.put("isolationModes", List.of("docker_isolated", "local_fallback", "unknown"));
        body.put("executions", executions);
        return body;
    }

    @GetMapping({"/policy", "/policies"})
    public Map<String, Object> policyLearning(@RequestParam(defaultValue = "50") int limit) {
        int cap = Math.max(1, Math.min(limit, 200));
        List<PolicyBundleRow> bundles = policyService.listActiveBundles();
        List<PolicyStatisticsRow> stats = policyService.listStatistics(null);
        List<PolicyRewardRow> rewards = policyRewardMapper.selectList(
                new QueryWrapper<PolicyRewardRow>().orderByDesc("create_time").last("limit " + cap));
        List<Map<String, Object>> rewardRows = rewards.stream().map(row -> {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("id", row.getId());
            item.put("runId", row.getRunId());
            item.put("policyId", row.getPolicyId());
            item.put("taskCategory", row.getTaskCategory());
            item.put("source", row.getSource());
            item.put("feedbackId", row.getFeedbackId());
            item.put("totalReward", row.getTotalReward());
            item.put("components", opsDebugService.parseJson(row.getComponents()));
            item.put("createTime", row.getCreateTime());
            return item;
        }).toList();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("mode", "policy_optimization_lab_no_gpu");
        body.put("label", "Policy Optimization Lab（无 GPU）");
        body.put("description",
                "Policy Optimization Lab（无 GPU）：ONLINE_SELECTION 生产仅 champion；"
                        + "bandit 探索仅 shadow/lab；OFFLINE_SEARCH 为有界配置进化（非完整 GEPA）；"
                        + "MODEL_WEIGHTS unchanged。");
        body.put("axes", Map.of(
                "ONLINE_SELECTION", "champion-only production / shadow bandit",
                "OFFLINE_SEARCH", "reflective evolutionary search (bounded, not full GEPA)",
                "MODEL_WEIGHTS", "unchanged",
                "SANDBOX", "experiment isolation"));
        body.put("bundles", bundles.stream().map(b -> {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("policyId", b.getPolicyId());
            item.put("name", b.getName());
            item.put("description", b.getDescription());
            item.put("status", b.getStatus());
            item.put("isChampion", b.getIsChampion());
            item.put("generation", b.getGeneration());
            item.put("version", b.getVersion());
            item.put("parentPolicyId", b.getParentPolicyId());
            return item;
        }).toList());
        body.put("statistics", stats);
        body.put("recentRewards", rewardRows);
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

    private String isolationMode(SandboxExecutionRow row) {
        if (row.getContainerId() != null && !row.getContainerId().isBlank()) {
            return "docker_isolated";
        }
        String status = row.getStatus() == null ? "" : row.getStatus().toLowerCase();
        if (status.contains("local") || status.contains("fallback")) {
            return "local_fallback";
        }
        return row.getSandboxId() != null && row.getSandboxId().startsWith("local")
                ? "local_fallback" : "unknown";
    }

    private String truncate(String value, int max) {
        if (value == null) {
            return null;
        }
        String normalized = value.replaceAll("\\s+", " ").trim();
        return normalized.length() <= max ? normalized : normalized.substring(0, max) + "…";
    }
}
