package com.resumai.agent.api;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.ai.SkillDescriptor;
import com.resumai.agent.ai.SkillProvider;
import com.resumai.agent.dao.MemoryEntryMapper;
import com.resumai.agent.dao.PolicyRewardMapper;
import com.resumai.agent.dao.SandboxExecutionMapper;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicyRewardRow;
import com.resumai.agent.domain.entity.PolicyStatisticsRow;
import com.resumai.agent.domain.entity.SandboxExecutionRow;
import com.resumai.agent.service.AgentMemoryService;
import com.resumai.agent.service.run.PolicyService;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.springframework.core.io.ClassPathResource;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Read-only Agent Ops surfaces: Memory / Sandbox / policy learning / MCP / Skills.
 */
@RestController
@RequestMapping("/api/ops")
public class OpsController {

    private final MemoryEntryMapper memoryEntryMapper;
    private final AgentMemoryService agentMemoryService;
    private final SandboxExecutionMapper sandboxExecutionMapper;
    private final PolicyService policyService;
    private final PolicyRewardMapper policyRewardMapper;
    private final SkillProvider skillProvider;
    private final ObjectMapper objectMapper;

    public OpsController(MemoryEntryMapper memoryEntryMapper,
                         AgentMemoryService agentMemoryService,
                         SandboxExecutionMapper sandboxExecutionMapper,
                         PolicyService policyService,
                         PolicyRewardMapper policyRewardMapper,
                         SkillProvider skillProvider,
                         ObjectMapper objectMapper) {
        this.memoryEntryMapper = memoryEntryMapper;
        this.agentMemoryService = agentMemoryService;
        this.sandboxExecutionMapper = sandboxExecutionMapper;
        this.policyService = policyService;
        this.policyRewardMapper = policyRewardMapper;
        this.skillProvider = skillProvider;
        this.objectMapper = objectMapper;
    }

    @GetMapping
    public Map<String, Object> overview() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("panels", List.of("memory", "sandbox", "policy", "mcp", "skills"));
        body.put("memory", memory(40));
        body.put("sandbox", sandbox(40));
        body.put("policy", policyLearning(40));
        body.put("mcp", mcp());
        body.put("skills", skills());
        return body;
    }

    @GetMapping("/memory")
    public Map<String, Object> memory(@RequestParam(defaultValue = "50") int limit) {
        int cap = Math.max(1, Math.min(limit, 200));
        List<MemoryEntryRow> entries = memoryEntryMapper.selectList(new QueryWrapper<MemoryEntryRow>()
                .orderByDesc("update_time")
                .last("limit " + cap));
        List<Map<String, Object>> preferences = new ArrayList<>();
        List<Map<String, Object>> summaries = new ArrayList<>();
        List<Map<String, Object>> hits = new ArrayList<>();
        Map<String, Long> byType = new LinkedHashMap<>();
        for (MemoryEntryRow row : entries) {
            Map<String, Object> item = memoryItem(row);
            String type = row.getType() == null ? "UNKNOWN" : row.getType();
            byType.merge(type, 1L, Long::sum);
            if ("PREFERENCE".equalsIgnoreCase(type)) {
                preferences.add(item);
            } else if ("CONVERSATION".equalsIgnoreCase(type) || "EPISODIC".equalsIgnoreCase(type)) {
                summaries.add(item);
            } else {
                hits.add(item);
            }
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", entries.size());
        body.put("byType", byType);
        body.put("preferences", preferences);
        body.put("summaries", summaries);
        body.put("hits", hits);
        body.put("fileStore", agentMemoryService.overview());
        return body;
    }

    @GetMapping("/sandbox")
    public Map<String, Object> sandbox(@RequestParam(defaultValue = "50") int limit) {
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
        body.put("count", executions.size());
        body.put("byStatus", byStatus);
        body.put("isolationModes", List.of("docker_isolated", "local_fallback", "unknown"));
        body.put("executions", executions);
        return body;
    }

    @GetMapping("/policy")
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
            item.put("components", parseJson(row.getComponents()));
            item.put("createTime", row.getCreateTime());
            return item;
        }).toList();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("mode", "rule_reward_no_gpu");
        body.put("description", "无 GPU RL：基于 feedback/reward 表的规则加权与策略统计可视化");
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
    public Map<String, Object> mcp() {
        JsonNode root = loadMcpConfig();
        List<Map<String, Object>> servers = new ArrayList<>();
        appendMcpServers(servers, root.path("mcpServers"), false);
        appendMcpServers(servers, root.path("optionalMcpServers"), true);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("evidencePolicy", root.path("evidencePolicy"));
        body.put("servers", servers);
        body.put("statusEnum", List.of("AVAILABLE", "RATE_LIMITED", "AUTH_REQUIRED", "DOWN"));
        return body;
    }

    @GetMapping("/skills")
    public Map<String, Object> skills() {
        List<Map<String, Object>> items = new ArrayList<>();
        List<Map<String, Object>> triggerMatrix = new ArrayList<>();
        for (SkillDescriptor skill : skillProvider.listInstalled()) {
            String hash = skillHash(skill);
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("name", skill.name());
            item.put("description", skill.description());
            item.put("allowedTools", skill.allowedTools());
            item.put("metadata", skill.metadata());
            item.put("directory", skill.directory() == null ? null : skill.directory().toString());
            item.put("version", skill.metadata().getOrDefault("version", "1"));
            item.put("hash", hash);
            item.put("manifest", Map.of(
                    "name", skill.name(),
                    "description", skill.description(),
                    "allowedTools", skill.allowedTools(),
                    "metadata", skill.metadata()));
            items.add(item);

            Map<String, Object> trigger = new LinkedHashMap<>();
            trigger.put("skill", skill.name());
            trigger.put("triggers", parseTriggers(skill.metadata()));
            trigger.put("agents", parseListMeta(skill.metadata(), "agents", "agent"));
            trigger.put("phases", parseListMeta(skill.metadata(), "phases", "phase"));
            trigger.put("tools", skill.allowedTools());
            triggerMatrix.add(trigger);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", items.size());
        body.put("skills", items);
        body.put("triggerMatrix", triggerMatrix);
        body.put("advertisedTools", List.of("load_skill", "execute_skill", "read_skill_resource"));
        return body;
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

    private String isolationMode(SandboxExecutionRow row) {
        if (row.getContainerId() != null && !row.getContainerId().isBlank()) {
            return "docker_isolated";
        }
        String status = row.getStatus() == null ? "" : row.getStatus().toLowerCase(Locale.ROOT);
        if (status.contains("local") || status.contains("fallback")) {
            return "local_fallback";
        }
        return row.getSandboxId() != null && row.getSandboxId().startsWith("local")
                ? "local_fallback" : "unknown";
    }

    private void appendMcpServers(List<Map<String, Object>> out, JsonNode node, boolean optional) {
        if (node == null || !node.isObject()) {
            return;
        }
        node.fields().forEachRemaining(entry -> {
            JsonNode cfg = entry.getValue();
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("name", entry.getKey());
            item.put("optional", optional);
            item.put("enabled", cfg.path("enabled").asBoolean(false));
            item.put("default", cfg.path("default").asBoolean(false));
            item.put("transport", textOrNull(cfg, "transport"));
            item.put("url", textOrNull(cfg, "url"));
            item.put("command", textOrNull(cfg, "command"));
            item.put("description", textOrNull(cfg, "description"));
            item.put("requiredEnv", readStringArray(cfg.path("requiredEnv")));
            item.put("optionalEnv", readStringArray(cfg.path("optionalEnv")));
            item.put("tools", inferMcpTools(cfg));
            item.put("status", resolveMcpStatus(cfg, optional));
            out.add(item);
        });
    }

    private String resolveMcpStatus(JsonNode cfg, boolean optional) {
        boolean enabled = cfg.path("enabled").asBoolean(false);
        List<String> required = readStringArray(cfg.path("requiredEnv"));
        boolean missingAuth = required.stream().anyMatch(env -> {
            String value = System.getenv(env);
            return value == null || value.isBlank();
        });
        if (!enabled) {
            return missingAuth && !required.isEmpty() ? "AUTH_REQUIRED" : "DOWN";
        }
        if (missingAuth) {
            return "AUTH_REQUIRED";
        }
        String desc = cfg.path("description").asText("").toLowerCase(Locale.ROOT);
        if (desc.contains("rate limit") || desc.contains("rate-limited")) {
            return "RATE_LIMITED";
        }
        if (optional && !cfg.path("default").asBoolean(false)) {
            return "AVAILABLE";
        }
        return "AVAILABLE";
    }

    private List<String> inferMcpTools(JsonNode cfg) {
        String url = cfg.path("url").asText("");
        int idx = url.indexOf("tools=");
        if (idx >= 0) {
            String tools = url.substring(idx + 6);
            int amp = tools.indexOf('&');
            if (amp >= 0) tools = tools.substring(0, amp);
            return List.of(tools.split(","));
        }
        String name = cfg.path("description").asText("");
        if (name.toLowerCase(Locale.ROOT).contains("search")) {
            return List.of("search");
        }
        return List.of();
    }

    private List<String> parseTriggers(Map<String, String> metadata) {
        if (metadata == null) return List.of();
        for (String key : List.of("triggers", "trigger", "when")) {
            if (metadata.containsKey(key)) {
                return splitCsv(metadata.get(key));
            }
        }
        return List.of("on_demand");
    }

    private List<String> parseListMeta(Map<String, String> metadata, String primary, String secondary) {
        if (metadata == null) return List.of();
        if (metadata.containsKey(primary)) return splitCsv(metadata.get(primary));
        if (metadata.containsKey(secondary)) return splitCsv(metadata.get(secondary));
        return List.of();
    }

    private List<String> splitCsv(String raw) {
        if (raw == null || raw.isBlank()) return List.of();
        List<String> out = new ArrayList<>();
        for (String part : raw.split("[,;|/]+")) {
            String trimmed = part.trim();
            if (!trimmed.isEmpty()) out.add(trimmed);
        }
        return out;
    }

    private String skillHash(SkillDescriptor skill) {
        try {
            Path skillMd = skill.directory().resolve("SKILL.md");
            byte[] bytes = Files.readAllBytes(skillMd);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(bytes)).substring(0, 12);
        } catch (Exception e) {
            return "unknown";
        }
    }

    private JsonNode loadMcpConfig() {
        try {
            return objectMapper.readTree(new ClassPathResource("mcp-servers.json").getInputStream());
        } catch (Exception e) {
            return objectMapper.createObjectNode();
        }
    }

    private Object parseJson(String raw) {
        if (raw == null || raw.isBlank()) return Map.of();
        try {
            return objectMapper.readTree(raw);
        } catch (Exception e) {
            return raw;
        }
    }

    private List<String> readStringArray(JsonNode node) {
        if (node == null || !node.isArray()) return List.of();
        List<String> out = new ArrayList<>();
        node.forEach(n -> {
            if (n.isTextual()) out.add(n.asText());
        });
        return out;
    }

    private String textOrNull(JsonNode node, String field) {
        JsonNode value = node.path(field);
        return value.isMissingNode() || value.isNull() || value.asText().isBlank() ? null : value.asText();
    }

    private String truncate(String value, int max) {
        if (value == null) return null;
        String normalized = value.replaceAll("\\s+", " ").trim();
        return normalized.length() <= max ? normalized : normalized.substring(0, max) + "…";
    }
}
