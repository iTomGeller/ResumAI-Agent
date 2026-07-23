package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.MemoryEntryMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import jakarta.annotation.PostConstruct;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Layered agent memory store (conversation / episodic / preference / failure)
 * with strict scope/source/consumer isolation so control-plane failures and
 * benchmark seeds never pollute candidate evaluation.
 */
@Service
public class MemoryService {

    private static final Logger log = LoggerFactory.getLogger(MemoryService.class);

    /**
     * Four memory types, each anchored to a unique consumer:
     * CONVERSATION (context injection), EPISODIC (score calibration),
     * PREFERENCE (user/HR stated preferences — source distinguishes
     * hr_feedback vs user_stated), FAILURE (planning hints + evolution loop).
     * WORKING (duplicate of the in-run blackboard) and DOMAIN (belongs to the
     * knowledge base) were removed in V8; legacy writes are remapped below.
     */
    public static final Set<String> TYPES = Set.of(
            "CONVERSATION", "EPISODIC", "PREFERENCE", "FAILURE");
    public static final Set<String> SCOPES = Set.of("RUN", "CONVERSATION", "USER", "GLOBAL");

    /** Default retrieval types for specialists / Report / Risk. */
    public static final List<String> SPECIALIST_TYPES = List.of(
            "CONVERSATION", "EPISODIC", "PREFERENCE");
    /** Scopes visible to evaluation specialists by default. */
    public static final Set<String> EVALUATION_SCOPES = Set.of("USER", "CONVERSATION");

    /** Control-plane error codes that must never enter Report/Risk context. */
    public static final Set<String> CONTROL_PLANE_ERROR_CODES = Set.of(
            "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED", "START_STUCK");

    private static final Set<String> FAILURE_CONSUMERS = Set.of(
            "COORDINATORAGENT", "COORDINATOR", "POLICYEVOLUTION", "POLICY_EVOLUTION",
            "POLICY-LAB", "POLICYLAB");

    private static final Map<String, String> LEGACY_TYPE_REMAP = Map.of(
            "USER_PREFERENCE", "PREFERENCE",
            "HR_FEEDBACK", "PREFERENCE",
            "WORKING", "CONVERSATION",
            "DOMAIN", "CONVERSATION");

    private static final Pattern SECRET_PATTERN = Pattern.compile(
            "(sk-[A-Za-z0-9]{16,}|Bearer\\s+[A-Za-z0-9._-]{16,}|password\\s*[:=]\\s*\\S+"
                    + "|api[_-]?key\\s*[:=]\\s*\\S+|AKID[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{12,})",
            Pattern.CASE_INSENSITIVE);

    /** Matches exp5_benchmark / exp_benchmark / exp12_benchmark style sources. */
    private static final Pattern BENCHMARK_SOURCE_PATTERN = Pattern.compile(
            "^exp\\d*_benchmark$", Pattern.CASE_INSENSITIVE);

    private final MemoryEntryMapper memoryMapper;
    private final ObjectMapper objectMapper;
    private final MemoryVectorService vectorService;

    public MemoryService(MemoryEntryMapper memoryMapper, ObjectMapper objectMapper,
                         MemoryVectorService vectorService) {
        this.memoryMapper = memoryMapper;
        this.objectMapper = objectMapper;
        this.vectorService = vectorService;
    }

    @PostConstruct
    public void archiveBenchmarkOnStartup() {
        try {
            int n = archiveBenchmarkMemories();
            if (n > 0) {
                log.info("archived {} benchmark memory entries on startup", n);
            }
        } catch (Exception e) {
            log.debug("benchmark memory archive skipped: {}", e.getMessage());
        }
    }

    public record WriteRequest(String type, String ownerScope, String userId, String conversationId,
                               String runId, String content, Map<String, Object> structuredContent,
                               String source, String sourceId, Double confidence,
                               String sensitivityLevel, Integer ttlDays) {
    }

    public MemoryEntryRow write(WriteRequest request) {
        String rawType = request.type() != null
                ? request.type().trim().toUpperCase(Locale.ROOT) : "";
        String type = normalize(LEGACY_TYPE_REMAP.getOrDefault(rawType, rawType),
                TYPES, "type");
        String scope = normalize(request.ownerScope(), SCOPES, "ownerScope");
        // PREFERENCE must never be written as GLOBAL — that would leak HR/user
        // preferences across all tenants into every evaluation.
        if ("PREFERENCE".equals(type) && "GLOBAL".equals(scope)) {
            log.warn("rejecting PREFERENCE GLOBAL write; coercing to USER");
            scope = "USER";
            if (!StringUtils.hasText(request.userId())) {
                throw new IllegalArgumentException(
                        "PREFERENCE requires USER/CONVERSATION scope with userId");
            }
        }
        String content = redactSecrets(request.content());
        if (!StringUtils.hasText(content)) {
            throw new IllegalArgumentException("memory content required");
        }
        String source = StringUtils.hasText(request.source()) ? request.source() : "model_generated";
        if (isBenchmarkSource(source) && "GLOBAL".equals(scope)) {
            // Keep benchmark seeds out of the global namespace.
            scope = "USER";
        }
        String hash = sha256(type + "|" + scope + "|" + scopeKey(request, scope)
                + "|" + normalizeForHash(content));

        // Dedup: identical content in the same scope bumps confidence instead
        // of creating another row.
        MemoryEntryRow existing = memoryMapper.selectOne(new QueryWrapper<MemoryEntryRow>()
                .eq("content_hash", hash).eq("status", "ACTIVE").last("limit 1"));
        if (existing != null) {
            double bumped = Math.min(1.0,
                    existing.getConfidence().doubleValue() * 0.7
                            + (request.confidence() != null ? request.confidence() : 0.5) * 0.3 + 0.05);
            existing.setConfidence(decimal(bumped));
            existing.setVersion(existing.getVersion() + 1);
            existing.setUpdateTime(LocalDateTime.now());
            memoryMapper.updateById(existing);
            return existing;
        }

        // Conflict handling: same fact key in the same scope with different
        // content marks the older entry CONFLICTED (no silent overwrite).
        String factKey = factKey(request);
        if (factKey != null) {
            List<MemoryEntryRow> sameFact = memoryMapper.selectList(new QueryWrapper<MemoryEntryRow>()
                    .eq("type", type).eq("owner_scope", scope).eq("status", "ACTIVE")
                    .eq("source_id", factKey)
                    .last("limit 10"));
            for (MemoryEntryRow oldEntry : sameFact) {
                if (!normalizeForHash(oldEntry.getContent()).equals(normalizeForHash(content))) {
                    oldEntry.setStatus("CONFLICTED");
                    oldEntry.setUpdateTime(LocalDateTime.now());
                    memoryMapper.updateById(oldEntry);
                }
            }
        }

        MemoryEntryRow row = new MemoryEntryRow();
        row.setMemoryId("mem-" + UUID.randomUUID());
        row.setType(type);
        row.setOwnerScope(scope);
        row.setUserId(request.userId());
        row.setConversationId(request.conversationId());
        row.setRunId(request.runId());
        row.setContent(content);
        row.setStructuredContent(writeJson(request.structuredContent()));
        row.setContentHash(hash);
        row.setSource(source);
        row.setSourceId(factKey != null ? factKey : request.sourceId());
        row.setConfidence(decimal(request.confidence() != null ? request.confidence() : 0.5));
        row.setStatus("ACTIVE");
        row.setVersion(1);
        row.setSensitivityLevel(StringUtils.hasText(request.sensitivityLevel())
                ? request.sensitivityLevel() : "NORMAL");
        LocalDateTime now = LocalDateTime.now();
        if (request.ttlDays() != null && request.ttlDays() > 0) {
            row.setExpiresAt(now.plusDays(request.ttlDays()));
        } else if ("RUN".equals(scope)) {
            // RUN-scoped short-term memory always expires quickly.
            row.setExpiresAt(now.plusDays(2));
        }
        row.setCreateTime(now);
        row.setUpdateTime(now);
        memoryMapper.insert(row);
        vectorService.indexAsync(row.getMemoryId(), content);
        return row;
    }

    public record SearchRequest(String query, List<String> types, String userId,
                                String conversationId, String runId, Integer topK,
                                Double minConfidence, Boolean includeSensitive,
                                /** EXP-5 ablation: fused (default) | lexical | semantic */
                                String channel,
                                /** Agent that will consume hits (drives type/scope whitelist). */
                                String consumerAgent,
                                /** When true, include exp*_benchmark sources (lab only). */
                                Boolean includeBenchmarkSources) {

        public SearchRequest(String query, List<String> types, String userId,
                             String conversationId, String runId, Integer topK,
                             Double minConfidence, Boolean includeSensitive) {
            this(query, types, userId, conversationId, runId, topK,
                    minConfidence, includeSensitive, null, null, null);
        }

        public SearchRequest(String query, List<String> types, String userId,
                             String conversationId, String runId, Integer topK,
                             Double minConfidence, Boolean includeSensitive,
                             String channel) {
            this(query, types, userId, conversationId, runId, topK,
                    minConfidence, includeSensitive, channel, null, null);
        }
    }

    /**
     * Scope/source/consumer-isolated retrieval.
     * Ranking = lexical overlap x confidence x recency decay.
     */
    public List<Map<String, Object>> search(SearchRequest request) {
        boolean allowFailure = allowsFailure(request.consumerAgent());
        boolean includeBenchmark = Boolean.TRUE.equals(request.includeBenchmarkSources());
        List<String> effectiveTypes = resolveTypes(request.types(), allowFailure);
        boolean evaluationConsumer = !allowFailure;

        if (evaluationConsumer
                && !StringUtils.hasText(request.userId())
                && !StringUtils.hasText(request.conversationId())) {
            return List.of();
        }

        QueryWrapper<MemoryEntryRow> query = new QueryWrapper<MemoryEntryRow>()
                .eq("status", "ACTIVE")
                .and(w -> {
                    boolean started = false;
                    if (!evaluationConsumer) {
                        w.eq("owner_scope", "GLOBAL");
                        started = true;
                    }
                    if (StringUtils.hasText(request.userId())) {
                        if (started) {
                            w.or(u -> u.eq("owner_scope", "USER").eq("user_id", request.userId()));
                        } else {
                            w.eq("owner_scope", "USER").eq("user_id", request.userId());
                            started = true;
                        }
                    }
                    if (StringUtils.hasText(request.conversationId())) {
                        if (started) {
                            w.or(c -> c.eq("owner_scope", "CONVERSATION")
                                    .eq("conversation_id", request.conversationId()));
                        } else {
                            w.eq("owner_scope", "CONVERSATION")
                                    .eq("conversation_id", request.conversationId());
                            started = true;
                        }
                    }
                    // RUN scope is short-term working memory; specialists stay on
                    // USER/CONVERSATION only. Coordinator may still see RUN.
                    if (!evaluationConsumer && StringUtils.hasText(request.runId())) {
                        w.or(r -> r.eq("owner_scope", "RUN").eq("run_id", request.runId()));
                    }
                });
        if (!effectiveTypes.isEmpty()) {
            query.in("type", effectiveTypes);
        }
        query.orderByDesc("update_time").last("limit 400");
        List<MemoryEntryRow> candidates = memoryMapper.selectList(query);

        double minConfidence = request.minConfidence() != null ? request.minConfidence() : 0.0;
        boolean includeSensitive = Boolean.TRUE.equals(request.includeSensitive());
        Set<String> queryTerms = terms(request.query());
        // Two-way recall: semantic scores from Milvus are fused with the
        // lexical path; the DB-side scope/status/confidence filter above stays
        // authoritative so a vector hit can never cross scope boundaries.
        Map<String, Double> vectorScores = vectorService.recall(request.query(), 20);
        List<Map.Entry<MemoryEntryRow, Double>> scored = new ArrayList<>();
        LocalDateTime now = LocalDateTime.now();
        for (MemoryEntryRow row : candidates) {
            if (row.getConfidence().doubleValue() < minConfidence) {
                continue;
            }
            if (!includeSensitive && "SENSITIVE".equals(row.getSensitivityLevel())) {
                continue;
            }
            if (row.getExpiresAt() != null && row.getExpiresAt().isBefore(now)) {
                continue;
            }
            if (!includeBenchmark && isBenchmarkSource(row.getSource())) {
                continue;
            }
            if (evaluationConsumer && !EVALUATION_SCOPES.contains(row.getOwnerScope())) {
                continue;
            }
            if ("FAILURE".equals(row.getType()) && !allowFailure) {
                continue;
            }
            if (isControlPlaneFailure(row) && !allowFailure) {
                continue;
            }
            // Report/Risk (and any evaluation consumer) never see control-plane
            // restart/start failures even if somehow typed differently.
            if (isControlPlaneFailure(row) && isReportOrRisk(request.consumerAgent())) {
                continue;
            }
            double lexical = overlap(queryTerms, terms(row.getContent()));
            double ageDays = Math.max(0,
                    Duration.between(row.getUpdateTime(), now).toHours() / 24.0);
            double recency = Math.exp(-ageDays / 30.0);
            double semantic = vectorScores.getOrDefault(row.getMemoryId(), 0.0);
            double relevance = switch (request.channel() == null ? "fused" : request.channel()) {
                case "lexical" -> lexical;
                case "semantic" -> semantic;
                default -> Math.max(lexical, semantic);
            };
            double score = (0.25 + 0.75 * relevance)
                    * row.getConfidence().doubleValue() * (0.3 + 0.7 * recency);
            scored.add(Map.entry(row, score));
        }
        scored.sort((a, b) -> Double.compare(b.getValue(), a.getValue()));
        int topK = request.topK() != null ? Math.min(Math.max(request.topK(), 1), 20) : 5;
        List<Map<String, Object>> out = new ArrayList<>();
        for (var entry : scored.subList(0, Math.min(topK, scored.size()))) {
            MemoryEntryRow row = entry.getKey();
            Map<String, Object> view = new LinkedHashMap<>();
            view.put("memoryId", row.getMemoryId());
            view.put("type", row.getType());
            view.put("ownerScope", row.getOwnerScope());
            view.put("scope", row.getOwnerScope());
            view.put("content", row.getContent());
            view.put("structuredContent", readJson(row.getStructuredContent()));
            view.put("source", row.getSource());
            view.put("sourceId", row.getSourceId());
            view.put("confidence", row.getConfidence());
            view.put("score", Math.round(entry.getValue() * 1000.0) / 1000.0);
            view.put("relevance", Map.of(
                    "lexical", Math.round(overlap(queryTerms, terms(row.getContent())) * 1000.0) / 1000.0,
                    "semantic", Math.round(vectorScores.getOrDefault(row.getMemoryId(), 0.0) * 1000.0) / 1000.0,
                    "fused", Math.round(entry.getValue() * 1000.0) / 1000.0));
            view.put("updatedAt", String.valueOf(row.getUpdateTime()));
            if (StringUtils.hasText(request.consumerAgent())) {
                view.put("consumerAgent", request.consumerAgent());
            }
            out.add(view);
        }
        return out;
    }

    public boolean userDelete(String memoryId, String userId) {
        UpdateWrapper<MemoryEntryRow> update = new UpdateWrapper<>();
        update.eq("memory_id", memoryId)
                .and(w -> w.eq("user_id", userId).or().eq("owner_scope", "GLOBAL"))
                .set("status", "DELETED")
                .set("update_time", LocalDateTime.now());
        return memoryMapper.update(null, update) > 0;
    }

    /** Archive RUN-scoped short-term memory of a finished run. */
    public void archiveRunWorkingMemory(String runId) {
        UpdateWrapper<MemoryEntryRow> update = new UpdateWrapper<>();
        update.eq("run_id", runId).eq("owner_scope", "RUN").eq("status", "ACTIVE")
                .set("status", "ARCHIVED")
                .set("update_time", LocalDateTime.now());
        memoryMapper.update(null, update);
    }

    /**
     * Archive experiment/benchmark seeded memories so production retrieval
     * cannot surface them even if the SQL REGEXP filter is bypassed.
     */
    public int archiveBenchmarkMemories() {
        UpdateWrapper<MemoryEntryRow> update = new UpdateWrapper<>();
        update.eq("status", "ACTIVE")
                .apply("source REGEXP {0}", "^exp[0-9]*_benchmark$")
                .set("status", "ARCHIVED")
                .set("update_time", LocalDateTime.now());
        return memoryMapper.update(null, update);
    }

    /**
     * Terminal-run memory policy:
     * <ul>
     *   <li>FAILED / TIMED_OUT — archive RUN working memory only; never write
     *       EPISODIC for control-plane or business failures (FAILURE rows stay
     *       isolated via {@link #writeFailureMemory}).</li>
     *   <li>Success / partial — write EPISODIC only when shared_state holds
     *       business artifacts (resumeFacts / findings / report / coverage).</li>
     * </ul>
     */
    @SuppressWarnings("unchecked")
    public void writeRunEpisode(AgentRun run, String terminalStatus) {
        String status = terminalStatus == null ? "" : terminalStatus.trim().toUpperCase(Locale.ROOT);
        if ("FAILED".equals(status) || "TIMED_OUT".equals(status)) {
            archiveRunWorkingMemory(run.getRunId());
            return;
        }
        if (!hasBusinessArtifacts(run.getSharedState())) {
            archiveRunWorkingMemory(run.getRunId());
            return;
        }

        Map<String, Object> artifacts = extractArtifacts(run.getSharedState());
        Map<String, Object> finalReport = artifacts.get("finalReport") instanceof Map<?, ?>
                ? (Map<String, Object>) artifacts.get("finalReport") : Map.of();

        String recommendation = String.valueOf(finalReport.getOrDefault("recommendation", ""));
        List<?> dimensions = finalReport.get("dimensions") instanceof List<?> dims ? dims : List.of();
        List<?> strengths = finalReport.get("strengths") instanceof List<?> s ? s : List.of();
        List<?> risks = finalReport.get("risks") instanceof List<?> r ? r : List.of();
        List<?> interviewProbes = finalReport.get("interviewProbes") instanceof List<?> q ? q : List.of();

        StringBuilder dimText = new StringBuilder();
        for (Object d : dimensions) {
            if (d instanceof Map<?, ?> dim) {
                dimText.append(dim.get("name")).append("=")
                       .append(dim.get("score")).append("; ");
            }
        }
        StringBuilder riskText = new StringBuilder();
        for (Object r : risks) {
            if (r instanceof Map<?, ?> risk) {
                riskText.append(risk.get("claim")).append("(")
                        .append(risk.get("severity")).append("); ");
            }
        }

        Map<String, Object> structured = new LinkedHashMap<>();
        structured.put("runId", run.getRunId());
        structured.put("recommendation", recommendation);
        structured.put("dimensions", dimensions.stream().limit(5).toList());
        structured.put("riskCount", risks.size());
        structured.put("interviewProbeCount", interviewProbes.size());
        structured.put("runType", run.getRunType());
        structured.put("status", terminalStatus);

        String content;
        if (StringUtils.hasText(recommendation)) {
            content = "候选人评估结论: 推荐=" + recommendation
                    + " | 维度: " + trim(dimText.toString(), 200)
                    + " | 优势: " + trim(strengths.stream().limit(3)
                        .map(Object::toString).reduce("", (a, b) -> a + b + "; "), 150)
                    + " | 风险(" + risks.size() + "): " + trim(riskText.toString(), 150)
                    + " | 面试问题数: " + interviewProbes.size();
        } else {
            content = "评估执行: 类别=" + run.getRunType()
                    + " | 结果=" + terminalStatus
                    + " | 维度: " + trim(dimText.toString(), 200);
        }

        write(new WriteRequest("EPISODIC", "CONVERSATION", run.getUserId(),
                run.getConversationId(), run.getRunId(), content, structured,
                "evaluation_result", "episodic:" + run.getRunId(), 0.85, "NORMAL", 90));
        archiveRunWorkingMemory(run.getRunId());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> extractArtifacts(String sharedStateJson) {
        if (!StringUtils.hasText(sharedStateJson)) return Map.of();
        try {
            Map<String, Object> state = objectMapper.readValue(sharedStateJson,
                    new com.fasterxml.jackson.core.type.TypeReference<>() {});
            Object arts = state.get("artifacts");
            return arts instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    /** @deprecated Prefer {@link #writeRunEpisode}; kept for call-site compat. */
    public void writeEpisodicRunMemory(AgentRun run, String terminalStatus) {
        writeRunEpisode(run, terminalStatus);
    }

    /**
     * True when shared_state JSON contains non-empty business artifacts that
     * specialists may later use for score calibration.
     */
    boolean hasBusinessArtifacts(String sharedStateJson) {
        if (!StringUtils.hasText(sharedStateJson)) {
            return false;
        }
        try {
            Object parsed = objectMapper.readValue(sharedStateJson, Object.class);
            if (!(parsed instanceof Map<?, ?> root)) {
                return false;
            }
            Object artifactsObj = root.get("artifacts");
            if (!(artifactsObj instanceof Map<?, ?> artifacts) || artifacts.isEmpty()) {
                return false;
            }
            for (String key : List.of(
                    "resumeFacts", "parsedResume", "technicalFindings", "finalReport",
                    "jdCoverage", "jdMatches", "evidence", "risks", "conflicts",
                    "effectiveJd", "structuredReport")) {
                Object value = artifacts.get(key);
                if (value == null) {
                    continue;
                }
                if (value instanceof Map<?, ?> m && !m.isEmpty()) {
                    return true;
                }
                if (value instanceof List<?> list && !list.isEmpty()) {
                    return true;
                }
                if (value instanceof String s && StringUtils.hasText(s)) {
                    return true;
                }
            }
            return false;
        } catch (Exception e) {
            return false;
        }
    }

    public void writeFailureMemory(AgentRun run, String errorCode, String errorMessage) {
        Map<String, Object> structured = new LinkedHashMap<>();
        structured.put("runId", run.getRunId());
        structured.put("errorCode", errorCode);
        structured.put("policyId", run.getPolicyId());
        structured.put("runType", run.getRunType());
        boolean controlPlane = isControlPlaneErrorCode(errorCode);
        if (controlPlane) {
            structured.put("category", "CONTROL_PLANE");
        }
        String content = "失败记录: 类别=" + run.getRunType() + " | 错误=" + errorCode
                + " | 详情=" + trim(errorMessage, 300);
        // FAILURE stays GLOBAL for Coordinator / policy evolution only;
        // retrieval isolation prevents injection into Report/Risk.
        write(new WriteRequest("FAILURE", "GLOBAL", run.getUserId(), run.getConversationId(),
                run.getRunId(), content, structured,
                controlPlane ? "control_plane" : "system_rule",
                "failure:" + run.getRunId(), 0.9, "NORMAL", 60));
    }

    public void writeHrFeedbackMemory(AgentRun run, String comment, Map<String, Object> structured) {
        write(new WriteRequest("PREFERENCE", "CONVERSATION", run.getUserId(),
                run.getConversationId(), run.getRunId(),
                "HR 反馈: " + trim(comment, 500), structured, "hr_feedback",
                "feedback:" + run.getRunId() + ":" + System.currentTimeMillis(), 0.95, "NORMAL", null));
    }

    @Scheduled(fixedDelayString = "${resumai.memory.cleanup-interval-ms:600000}")
    public void cleanupExpired() {
        try {
            UpdateWrapper<MemoryEntryRow> update = new UpdateWrapper<>();
            update.eq("status", "ACTIVE")
                    .isNotNull("expires_at")
                    .lt("expires_at", LocalDateTime.now())
                    .set("status", "EXPIRED")
                    .set("update_time", LocalDateTime.now());
            int expired = memoryMapper.update(null, update);
            if (expired > 0) {
                log.info("memory cleanup expired {} entries", expired);
            }
            int archived = archiveBenchmarkMemories();
            if (archived > 0) {
                log.info("memory cleanup archived {} benchmark entries", archived);
            }
        } catch (Exception e) {
            log.debug("memory cleanup skipped: {}", e.getMessage());
        }
    }

    // ------------------------------------------------------------------

    static boolean isBenchmarkSource(String source) {
        if (!StringUtils.hasText(source)) {
            return false;
        }
        return BENCHMARK_SOURCE_PATTERN.matcher(source.trim()).matches();
    }

    static boolean isControlPlaneErrorCode(String errorCode) {
        if (!StringUtils.hasText(errorCode)) {
            return false;
        }
        return CONTROL_PLANE_ERROR_CODES.contains(errorCode.trim().toUpperCase(Locale.ROOT));
    }

    static boolean allowsFailure(String consumerAgent) {
        if (!StringUtils.hasText(consumerAgent)) {
            // Missing consumer defaults to evaluation-safe (no FAILURE / no GLOBAL).
            return false;
        }
        String key = consumerAgent.trim().toUpperCase(Locale.ROOT).replace("-", "").replace("_", "");
        if (FAILURE_CONSUMERS.contains(key) || FAILURE_CONSUMERS.contains(
                consumerAgent.trim().toUpperCase(Locale.ROOT))) {
            return true;
        }
        return "COORDINATORAGENT".equals(key) || key.startsWith("POLICY");
    }

    static boolean isReportOrRisk(String consumerAgent) {
        if (!StringUtils.hasText(consumerAgent)) {
            return false;
        }
        String key = consumerAgent.trim().toUpperCase(Locale.ROOT);
        return "REPORTAGENT".equals(key) || "RISKAGENT".equals(key);
    }

    private boolean isControlPlaneFailure(MemoryEntryRow row) {
        if (row == null) {
            return false;
        }
        if ("control_plane".equalsIgnoreCase(row.getSource())) {
            return true;
        }
        Object structured = readJson(row.getStructuredContent());
        if (structured instanceof Map<?, ?> map) {
            Object code = map.get("errorCode");
            if (code != null && isControlPlaneErrorCode(String.valueOf(code))) {
                return true;
            }
            Object category = map.get("category");
            if (category != null && "CONTROL_PLANE".equalsIgnoreCase(String.valueOf(category))) {
                return true;
            }
        }
        String content = row.getContent() != null ? row.getContent() : "";
        for (String code : CONTROL_PLANE_ERROR_CODES) {
            if (content.contains(code)) {
                return true;
            }
        }
        return false;
    }

    private List<String> resolveTypes(List<String> requested, boolean allowFailure) {
        List<String> base;
        if (requested == null || requested.isEmpty()) {
            base = new ArrayList<>(SPECIALIST_TYPES);
            if (allowFailure) {
                base.add("FAILURE");
            }
        } else {
            base = new ArrayList<>();
            for (String t : requested) {
                if (!StringUtils.hasText(t)) {
                    continue;
                }
                String normalized = t.trim().toUpperCase(Locale.ROOT);
                String remapped = LEGACY_TYPE_REMAP.getOrDefault(normalized, normalized);
                if (TYPES.contains(remapped)) {
                    base.add(remapped);
                }
            }
        }
        if (!allowFailure) {
            base.removeIf("FAILURE"::equals);
        }
        return base;
    }

    private String factKey(WriteRequest request) {
        if (request.structuredContent() != null
                && request.structuredContent().get("factKey") instanceof String key
                && StringUtils.hasText(key)) {
            return key;
        }
        return null;
    }

    private String scopeKey(WriteRequest request, String scope) {
        return String.join(":",
                String.valueOf(request.userId()),
                String.valueOf(request.conversationId()),
                "RUN".equals(scope) ? String.valueOf(request.runId()) : "-");
    }

    static String redactSecrets(String content) {
        if (content == null) {
            return null;
        }
        return SECRET_PATTERN.matcher(content).replaceAll("[REDACTED]");
    }

    private String normalize(String value, Set<String> allowed, String field) {
        String normalized = value != null ? value.trim().toUpperCase(Locale.ROOT) : "";
        if (!allowed.contains(normalized)) {
            throw new IllegalArgumentException("invalid " + field + ": " + value);
        }
        return normalized;
    }

    private static String normalizeForHash(String content) {
        return content.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
    }

    private static Set<String> terms(String text) {
        if (!StringUtils.hasText(text)) {
            return Set.of();
        }
        Set<String> out = new HashSet<>();
        for (String token : text.toLowerCase(Locale.ROOT)
                .split("[\\s,，。；;、:：()（）\\[\\]{}<>\"'|/\\\\]+")) {
            if (token.length() >= 2) {
                out.add(token);
            }
        }
        // Chinese text often lacks whitespace; add bigrams for overlap.
        String compact = text.replaceAll("\\s+", "");
        for (int i = 0; i + 2 <= compact.length() && i < 600; i++) {
            String bigram = compact.substring(i, i + 2);
            if (bigram.codePointAt(0) > 0x2E80) {
                out.add(bigram);
            }
        }
        return out;
    }

    private static double overlap(Set<String> query, Set<String> doc) {
        if (query.isEmpty() || doc.isEmpty()) {
            return 0.0;
        }
        long hit = query.stream().filter(doc::contains).count();
        return (double) hit / query.size();
    }

    private static String sha256(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte b : hash) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (Exception e) {
            return Integer.toHexString(value.hashCode());
        }
    }

    private BigDecimal decimal(double value) {
        return BigDecimal.valueOf(Math.max(0, Math.min(1, value)))
                .setScale(3, RoundingMode.HALF_UP);
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value != null ? value : Map.of());
        } catch (Exception e) {
            return "{}";
        }
    }

    private Object readJson(String json) {
        try {
            return StringUtils.hasText(json) ? objectMapper.readValue(json, Map.class) : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    private String trim(String text, int max) {
        if (text == null) {
            return "";
        }
        return text.length() > max ? text.substring(0, max) : text;
    }
}
