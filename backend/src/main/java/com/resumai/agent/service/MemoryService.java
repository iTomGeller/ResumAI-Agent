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
import java.time.Instant;
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
 * Layered agent memory store using the standard agent-memory taxonomy:
 * semantic facts, episodic execution experience, approved procedures and
 * run-scoped working memory. Scope/source/consumer isolation prevents one
 * candidate, benchmark seed or control-plane failure from contaminating
 * another candidate's evaluation.
 */
@Service
public class MemoryService {

    private static final Logger log = LoggerFactory.getLogger(MemoryService.class);

    /** Canonical taxonomy exposed to the runtime and trace UI. */
    public static final Set<String> TYPES = Set.of(
            "SEMANTIC", "EPISODIC", "PROCEDURAL", "WORKING");
    public static final Set<String> SCOPES = Set.of("RUN", "CONVERSATION", "USER", "GLOBAL");

    /** Default evaluation-safe pool; per-agent retrieval plans narrow this further. */
    public static final List<String> SPECIALIST_TYPES = List.of(
            "SEMANTIC", "EPISODIC", "PROCEDURAL");

    /** Control-plane error codes that must never enter Report/Risk context. */
    public static final Set<String> CONTROL_PLANE_ERROR_CODES = Set.of(
            "ORPHANED_ON_RESTART", "RUNTIME_START_FAILED", "START_STUCK");

    private static final Set<String> FAILURE_CONSUMERS = Set.of(
            "COORDINATORAGENT", "COORDINATOR", "POLICYEVOLUTION", "POLICY_EVOLUTION",
            "POLICY-LAB", "POLICYLAB");

    /**
     * Read/write compatibility for rows created before the canonical taxonomy.
     * FAILURE remains an episodic outcome, never a fifth memory taxonomy.
     */
    private static final Map<String, String> LEGACY_TYPE_REMAP = Map.ofEntries(
            Map.entry("CONVERSATION", "WORKING"),
            Map.entry("SHORT_TERM", "WORKING"),
            Map.entry("PREFERENCE", "SEMANTIC"),
            Map.entry("USER_PREFERENCE", "SEMANTIC"),
            Map.entry("HR_FEEDBACK", "SEMANTIC"),
            Map.entry("DOMAIN", "SEMANTIC"),
            Map.entry("FAILURE", "EPISODIC"));

    private static final Map<String, Set<String>> STORAGE_TYPES = Map.of(
            "SEMANTIC", Set.of("SEMANTIC", "PREFERENCE", "USER_PREFERENCE", "HR_FEEDBACK", "DOMAIN"),
            "EPISODIC", Set.of("EPISODIC", "FAILURE"),
            "PROCEDURAL", Set.of("PROCEDURAL"),
            "WORKING", Set.of("WORKING", "CONVERSATION", "SHORT_TERM"));

    private static final Set<String> TRUSTED_PROCEDURAL_SOURCES = Set.of(
            "approved_policy", "approved_skill", "policy_approval",
            "skill_registry", "system_approved");
    private static final String RUNTIME_STRATEGY_SOURCE = "runtime_strategy";

    private static final Pattern SECRET_PATTERN = Pattern.compile(
            "(sk-[A-Za-z0-9]{16,}|Bearer\\s+[A-Za-z0-9._-]{16,}|password\\s*[:=]\\s*\\S+"
                    + "|api[_-]?key\\s*[:=]\\s*\\S+|AKID[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{12,})",
            Pattern.CASE_INSENSITIVE);

    private static final Map<String, Duration> TTL_BY_TYPE = Map.of(
            "WORKING", Duration.ofDays(2),
            "SEMANTIC", Duration.ofDays(90),
            "EPISODIC", Duration.ofDays(90),
            "PROCEDURAL", Duration.ofDays(365));

    /** Effective type defaults exposed to Ops; writes may still override ttlDays. */
    public static Map<String, Long> ttlPolicyDays() {
        Map<String, Long> days = new LinkedHashMap<>();
        TTL_BY_TYPE.forEach((type, ttl) -> days.put(type, ttl.toDays()));
        return Map.copyOf(days);
    }

    public static long defaultTtlDays(String rawType) {
        String type = canonicalTaxonomy(rawType);
        return TTL_BY_TYPE.getOrDefault(type, Duration.ofDays(30)).toDays();
    }

    /** Matches exp5_benchmark / exp_benchmark / exp12_benchmark style sources. */
    private static final Pattern BENCHMARK_SOURCE_PATTERN = Pattern.compile(
            "^exp\\d*_benchmark$", Pattern.CASE_INSENSITIVE);
    private static final String PENDING_PROMOTION_KEY = "_pendingPromotion";
    private static final String RUNTIME_WRITE_KEY = "_runtimeWrite";
    private static final String RUNTIME_WRITE_ID_KEY = "writeId";
    private static final String RUNTIME_WRITE_KIND_KEY = "kind";
    private static final String RUNTIME_WORKING_KIND = "WORKING";
    private static final String RUNTIME_PROMOTION_KIND = "PENDING_PROMOTION";

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
        String type = normalize(canonicalTaxonomy(rawType), TYPES, "type");
        String scope = normalize(request.ownerScope(), SCOPES, "ownerScope");
        if ("WORKING".equals(type)) {
            // Scratch/checkpoint context belongs to exactly one run. New writes
            // never create long-lived "conversation memory" by accident.
            if (!StringUtils.hasText(request.runId())) {
                throw new IllegalArgumentException("WORKING memory requires runId");
            }
            scope = "RUN";
        }
        if ("SEMANTIC".equals(type) && "GLOBAL".equals(scope)) {
            // Candidate/job/user facts are tenant facts, not global truths.
            if (!StringUtils.hasText(request.userId())) {
                throw new IllegalArgumentException("SEMANTIC GLOBAL write requires a user namespace");
            }
            log.warn("coercing SEMANTIC GLOBAL write to USER scope");
            scope = "USER";
        }
        if ("PROCEDURAL".equals(type) && !isApprovedProcedure(request)) {
            throw new IllegalArgumentException(
                    "PROCEDURAL memory requires an approved policy/skill source");
        }
        validateScopeOwner(request, scope);
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
                + "|" + runtimeDedupDiscriminator(request, source)
                + "|" + normalizeForHash(content));

        // Dedup: identical content in the same scope bumps confidence instead
        // of creating another row.
        MemoryEntryRow existing = memoryMapper.selectOne(new QueryWrapper<MemoryEntryRow>()
                .eq("content_hash", hash).eq("status", "ACTIVE").last("limit 1"));
        if (existing != null) {
            double existingConfidence = existing.getConfidence() != null
                    ? existing.getConfidence().doubleValue() : 0.5;
            double bumped = Math.min(1.0,
                    existingConfidence * 0.7
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
            QueryWrapper<MemoryEntryRow> sameFactQuery = new QueryWrapper<MemoryEntryRow>()
                    .eq("type", type).eq("owner_scope", scope).eq("status", "ACTIVE")
                    .eq("source_id", factKey);
            addExactScope(sameFactQuery, request, scope);
            List<MemoryEntryRow> sameFact = memoryMapper.selectList(
                    sameFactQuery.last("limit 10"));
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
            row.setExpiresAt(now.plusDays(2));
        } else {
            Duration defaultTtl = TTL_BY_TYPE.getOrDefault(type, Duration.ofDays(30));
            row.setExpiresAt(now.plus(defaultTtl));
        }
        row.setCreateTime(now);
        row.setUpdateTime(now);
        memoryMapper.insert(row);
        vectorService.indexAsync(row.getMemoryId(), content);
        return row;
    }

    /**
     * Runtime writes are staged as RUN-scoped WORKING memory. Durable
     * SEMANTIC/EPISODIC/PROCEDURAL rows are created only after Java accepts a
     * successful terminal callback. This makes cancellation rollback complete
     * without a schema migration or cross-table transaction.
     */
    public MemoryEntryRow stageRuntimeWrite(WriteRequest request) {
        String targetType = normalize(
                canonicalTaxonomy(request.type()), TYPES, "type");
        String targetScope = normalize(request.ownerScope(), SCOPES, "ownerScope");
        if (!StringUtils.hasText(request.runId())) {
            throw new IllegalArgumentException(
                    "runtime memory staging requires runId");
        }
        Map<String, Object> structured = new LinkedHashMap<>();
        if (request.structuredContent() != null) {
            structured.putAll(request.structuredContent());
        }
        Map<String, Object> runtimeWrite = new LinkedHashMap<>();
        runtimeWrite.put(RUNTIME_WRITE_ID_KEY, "rw-" + UUID.randomUUID());
        runtimeWrite.put(RUNTIME_WRITE_KIND_KEY,
                "WORKING".equals(targetType)
                        ? RUNTIME_WORKING_KIND : RUNTIME_PROMOTION_KIND);
        structured.put(RUNTIME_WRITE_KEY, runtimeWrite);

        if ("WORKING".equals(targetType)) {
            /*
             * Runtime WORKING writes deliberately carry a per-call writeId.
             * The writeId participates in the hash, so this request cannot
             * deduplicate onto a pre-existing checkpoint/scratch row. The
             * controller can therefore compensate the pre/post-cancel race by
             * the returned memoryId without archiving anybody else's row.
             */
            return write(new WriteRequest(
                    "WORKING", "RUN", request.userId(), request.conversationId(),
                    request.runId(), request.content(), structured,
                    request.source(), request.sourceId(), request.confidence(),
                    request.sensitivityLevel(), request.ttlDays()));
        }

        Map<String, Object> promotion = new LinkedHashMap<>();
        promotion.put("type", targetType);
        promotion.put("ownerScope", targetScope);
        promotion.put("source", StringUtils.hasText(request.source())
                ? request.source() : "model_generated");
        if (StringUtils.hasText(request.sourceId())) {
            promotion.put("sourceId", request.sourceId());
        }
        if (request.ttlDays() != null) {
            promotion.put("ttlDays", request.ttlDays());
        }
        structured.put(PENDING_PROMOTION_KEY, promotion);
        return write(new WriteRequest(
                "WORKING", "RUN", request.userId(), request.conversationId(),
                request.runId(), request.content(), structured,
                request.source(), request.sourceId(), request.confidence(),
                request.sensitivityLevel(), 2));
    }

    /**
     * Promote staged durable memories after an accepted success, then archive
     * every remaining RUN scratch row. Failed promotions are fail-closed and
     * remain non-retrievable after archival.
     */
    public List<MemoryEntryRow> promoteRunMemories(String runId) {
        if (!StringUtils.hasText(runId)) {
            return List.of();
        }
        List<MemoryEntryRow> staged = memoryMapper.selectList(
                new QueryWrapper<MemoryEntryRow>()
                        .eq("run_id", runId)
                        .eq("owner_scope", "RUN")
                        .eq("type", "WORKING")
                        .eq("status", "ACTIVE"));
        List<MemoryEntryRow> promoted = new ArrayList<>();
        for (MemoryEntryRow row : staged) {
            Object decoded = readJson(row.getStructuredContent());
            if (!(decoded instanceof Map<?, ?> rawStructured)
                    || !(rawStructured.get(PENDING_PROMOTION_KEY)
                    instanceof Map<?, ?> pending)) {
                continue;
            }
            Map<String, Object> structured = new LinkedHashMap<>();
            rawStructured.forEach((key, value) -> {
                String name = String.valueOf(key);
                if (!PENDING_PROMOTION_KEY.equals(name)
                        && !RUNTIME_WRITE_KEY.equals(name)) {
                    structured.put(String.valueOf(key), value);
                }
            });
            String targetType = String.valueOf(pending.get("type"));
            String targetScope = String.valueOf(pending.get("ownerScope"));
            String sourceId = pending.get("sourceId") != null
                    ? String.valueOf(pending.get("sourceId")) : row.getSourceId();
            Integer ttlDays = pending.get("ttlDays") instanceof Number n
                    ? n.intValue() : null;
            try {
                MemoryEntryRow durable = write(new WriteRequest(
                        targetType, targetScope, row.getUserId(),
                        row.getConversationId(), row.getRunId(),
                        row.getContent(), structured, row.getSource(), sourceId,
                        row.getConfidence() != null
                                ? row.getConfidence().doubleValue() : 0.5,
                        row.getSensitivityLevel(), ttlDays));
                promoted.add(durable);
                row.setStatus("ARCHIVED");
                row.setUpdateTime(LocalDateTime.now());
                memoryMapper.updateById(row);
            } catch (Exception e) {
                log.warn("memory promotion rejected run={} memory={}: {}",
                        runId, row.getMemoryId(), e.getMessage());
            }
        }
        archiveRunWorkingMemory(runId);
        return promoted;
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
        boolean failureOnly = requestsLegacyFailure(request.types());
        RetrievalPlan retrievalPlan = retrievalPlan(
                request.types(), request.consumerAgent(), request.query());
        List<String> effectiveTypes = retrievalPlan.allowedTypes();

        boolean hasOwnedNamespace = StringUtils.hasText(request.userId())
                || StringUtils.hasText(request.conversationId())
                || StringUtils.hasText(request.runId());
        boolean mayReadGlobal = effectiveTypes.contains("PROCEDURAL") || allowFailure;
        if (!hasOwnedNamespace && !mayReadGlobal) {
            return List.of();
        }

        QueryWrapper<MemoryEntryRow> query = new QueryWrapper<MemoryEntryRow>()
                .eq("status", "ACTIVE")
                .and(w -> {
                    boolean started = false;
                    if (mayReadGlobal) {
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
                    if (StringUtils.hasText(request.runId())) {
                        if (started) {
                            w.or(r -> r.eq("owner_scope", "RUN")
                                    .eq("run_id", request.runId()));
                        } else {
                            w.eq("owner_scope", "RUN").eq("run_id", request.runId());
                            started = true;
                        }
                    }
                });
        Set<String> storedTypes = storageTypesFor(effectiveTypes);
        if (!storedTypes.isEmpty()) {
            query.in("type", storedTypes);
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
            String taxonomy = canonicalTaxonomy(row.getType());
            double confidence = row.getConfidence() != null
                    ? row.getConfidence().doubleValue() : 0.0;
            if (confidence < minConfidence) {
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
            if (!isVisibleInNamespace(row, taxonomy, request, allowFailure)) {
                continue;
            }
            boolean failureEpisode = isFailureEpisode(row);
            if (failureOnly && !failureEpisode) {
                continue;
            }
            if (failureEpisode && !allowFailure) {
                continue;
            }
            if (isControlPlaneFailure(row) && isReportOrRisk(request.consumerAgent())) {
                continue;
            }
            if ("PROCEDURAL".equals(taxonomy) && !isApprovedProcedure(row)) {
                continue;
            }
            double lexical = overlap(queryTerms, terms(row.getContent()));
            double ageDays = Math.max(0,
                    Duration.between(row.getUpdateTime(), now).toHours() / 24.0);
            double recency = Math.exp(-ageDays / recencyHalfLifeDays(taxonomy));
            double semantic = vectorScores.getOrDefault(row.getMemoryId(), 0.0);
            double relevance = switch (request.channel() == null ? "fused" : request.channel()) {
                case "lexical" -> lexical;
                case "semantic" -> semantic;
                default -> Math.max(lexical, semantic);
            };
            // A zero-overlap row is not a "hit". This prevents the old
            // always-100%-hit dashboard caused by the score floor alone.
            if (!queryTerms.isEmpty() && relevance < 0.05) {
                continue;
            }
            double typeAffinity = typeAffinity(taxonomy, retrievalPlan.preferredTypes());
            double score = (0.25 + 0.75 * relevance)
                    * confidence * (0.3 + 0.7 * recency) * typeAffinity;
            scored.add(Map.entry(row, score));
        }
        scored.sort((a, b) -> Double.compare(b.getValue(), a.getValue()));
        int topK = request.topK() != null ? Math.min(Math.max(request.topK(), 1), 20) : 5;
        List<Map<String, Object>> out = new ArrayList<>();
        String occurredAt = Instant.now().toString();
        for (var entry : scored.subList(0, Math.min(topK, scored.size()))) {
            MemoryEntryRow row = entry.getKey();
            String taxonomy = canonicalTaxonomy(row.getType());
            double lexical = overlap(queryTerms, terms(row.getContent()));
            double semantic = vectorScores.getOrDefault(row.getMemoryId(), 0.0);
            double ageDays = Math.max(0,
                    Duration.between(row.getUpdateTime(), now).toHours() / 24.0);
            double recency = Math.exp(-ageDays / recencyHalfLifeDays(taxonomy));
            Map<String, Object> view = new LinkedHashMap<>();
            view.put("memoryId", row.getMemoryId());
            view.put("type", taxonomy);
            view.put("memoryType", taxonomy);
            view.put("taxonomy", taxonomy);
            view.put("storedType", row.getType());
            view.put("ownerScope", row.getOwnerScope());
            view.put("scope", row.getOwnerScope());
            view.put("namespace", namespaceOf(row));
            view.put("content", row.getContent());
            view.put("structuredContent", readJson(row.getStructuredContent()));
            view.put("source", row.getSource());
            view.put("sourceId", row.getSourceId());
            view.put("confidence", row.getConfidence());
            view.put("score", Math.round(entry.getValue() * 1000.0) / 1000.0);
            view.put("relevance", Map.of(
                    "lexical", Math.round(lexical * 1000.0) / 1000.0,
                    "semantic", Math.round(semantic * 1000.0) / 1000.0,
                    "recency", Math.round(recency * 1000.0) / 1000.0,
                    "fused", Math.round(entry.getValue() * 1000.0) / 1000.0));
            view.put("updatedAt", String.valueOf(row.getUpdateTime()));
            view.put("occurredAt", occurredAt);
            view.put("selectionReason", retrievalPlan.reason());
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
     * Compensate the post-insert cancellation race for one runtime write.
     * The predicates are deliberately redundant: even if promotion wins the
     * race, a durable row or an archived staging row cannot be touched.
     */
    public boolean archiveRuntimeWrite(String runId, String memoryId) {
        if (!StringUtils.hasText(runId) || !StringUtils.hasText(memoryId)) {
            return false;
        }
        UpdateWrapper<MemoryEntryRow> update =
                runtimeWriteArchive(runId, memoryId);
        return memoryMapper.update(null, update) > 0;
    }

    /**
     * Backward-compatible name retained for callers compiled against the
     * initial durable-only staging implementation.
     */
    public boolean archivePendingRuntimeMemory(String runId, String memoryId) {
        return archiveRuntimeWrite(runId, memoryId);
    }

    /**
     * Roll back only pending durable-memory staging rows of a non-successful
     * run. Plain WORKING rows may contain resumable scratch/checkpoint context
     * and promoted durable rows have a different scope/type, so neither is
     * eligible for this compensation.
     */
    public int archiveRunProducedMemory(String runId) {
        if (!StringUtils.hasText(runId)) {
            return 0;
        }
        UpdateWrapper<MemoryEntryRow> update =
                pendingPromotionArchive(runId, null);
        return memoryMapper.update(null, update);
    }

    static UpdateWrapper<MemoryEntryRow> pendingPromotionArchive(
            String runId, String memoryId) {
        UpdateWrapper<MemoryEntryRow> update = new UpdateWrapper<>();
        update.eq("run_id", runId);
        if (StringUtils.hasText(memoryId)) {
            update.eq("memory_id", memoryId);
        }
        update.eq("owner_scope", "RUN")
                .eq("type", "WORKING")
                .eq("status", "ACTIVE")
                .apply("JSON_CONTAINS_PATH(structured_content, 'one', '$._pendingPromotion') = 1")
                .set("status", "ARCHIVED")
                .set("update_time", LocalDateTime.now());
        return update;
    }

    /**
     * Exact pre/post-check compensation for one HTTP runtime write. memory_id
     * identifies the returned row; the runtime marker prevents an ordinary
     * checkpoint/scratch row from being eligible even if a caller supplies a
     * wrong identifier.
     */
    static UpdateWrapper<MemoryEntryRow> runtimeWriteArchive(
            String runId, String memoryId) {
        UpdateWrapper<MemoryEntryRow> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .eq("memory_id", memoryId)
                .eq("owner_scope", "RUN")
                .eq("type", "WORKING")
                .eq("status", "ACTIVE")
                .apply("JSON_CONTAINS_PATH(structured_content, 'one', "
                        + "'$._runtimeWrite.writeId') = 1")
                .set("status", "ARCHIVED")
                .set("update_time", LocalDateTime.now());
        return update;
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
     *       EPISODIC for control-plane or business failures (failed outcomes
     *       stay isolated via source/category metadata).</li>
     *   <li>Success / partial — write EPISODIC only when shared_state holds
     *       business artifacts (resumeFacts / findings / report / coverage).</li>
     * </ul>
     */
    @SuppressWarnings("unchecked")
    public void writeRunEpisode(AgentRun run, String terminalStatus) {
        String status = terminalStatus == null ? "" : terminalStatus.trim().toUpperCase(Locale.ROOT);
        if (!shouldWriteRunEpisode(status, run.getSharedState())) {
            archiveRunProducedMemory(run.getRunId());
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

    boolean shouldWriteRunEpisode(String terminalStatus, String sharedStateJson) {
        String status = terminalStatus == null
                ? "" : terminalStatus.trim().toUpperCase(Locale.ROOT);
        if (Set.of("FAILED", "TIMED_OUT", "CANCELLED").contains(status)) {
            return false;
        }
        return hasBusinessArtifacts(sharedStateJson);
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
        structured.put("outcome", "FAILURE");
        String content = "失败记录: 类别=" + run.getRunType() + " | 错误=" + errorCode
                + " | 详情=" + trim(errorMessage, 300);
        // A failed run is still an EPISODIC event. GLOBAL scope is reserved
        // for Coordinator/policy learning and is excluded from evaluation.
        write(new WriteRequest("EPISODIC", "GLOBAL", run.getUserId(), run.getConversationId(),
                run.getRunId(), content, structured,
                controlPlane ? "control_plane" : "failed_run",
                "failure:" + run.getRunId(), 0.9, "NORMAL", 60));
    }

    public void writeHrFeedbackMemory(AgentRun run, String comment, Map<String, Object> structured) {
        write(new WriteRequest("SEMANTIC", "CONVERSATION", run.getUserId(),
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

    public record RetrievalPlan(List<String> allowedTypes,
                                List<String> preferredTypes,
                                String reason) {
    }

    /**
     * Agent- and query-aware taxonomy plan. The allow-list protects isolation;
     * the preference order changes ranking without hiding useful evidence.
     */
    static RetrievalPlan retrievalPlan(List<String> requested, String consumerAgent, String query) {
        List<String> allowed = new ArrayList<>();
        if (requested != null && !requested.isEmpty()) {
            for (String type : requested) {
                String canonical = canonicalTaxonomy(type);
                if (TYPES.contains(canonical) && !allowed.contains(canonical)) {
                    allowed.add(canonical);
                }
            }
        } else {
            String agent = normalizeAgent(consumerAgent);
            if (agent.contains("RESUMEPARSER") || agent.contains("CONVERSATION")) {
                allowed.addAll(List.of("SEMANTIC", "WORKING"));
            } else if (agent.contains("JDANALYSIS") || agent.contains("JDAGENT")) {
                allowed.addAll(List.of("SEMANTIC", "PROCEDURAL", "WORKING"));
            } else if (agent.contains("POLICY")) {
                allowed.addAll(List.of("PROCEDURAL", "EPISODIC"));
            } else if (agent.contains("COORDINATOR")) {
                allowed.addAll(List.of("WORKING", "SEMANTIC", "PROCEDURAL", "EPISODIC"));
            } else if (agent.contains("REPORT") || agent.contains("RISK")) {
                allowed.addAll(List.of("EPISODIC", "SEMANTIC", "PROCEDURAL"));
            } else {
                allowed.addAll(SPECIALIST_TYPES);
            }
        }
        if (allowed.isEmpty()) {
            allowed.addAll(SPECIALIST_TYPES);
        }

        List<String> inferred = inferQueryTypes(query);
        List<String> preferred = new ArrayList<>();
        for (String type : inferred) {
            if (allowed.contains(type) && !preferred.contains(type)) {
                preferred.add(type);
            }
        }
        for (String type : allowed) {
            if (!preferred.contains(type)) {
                preferred.add(type);
            }
        }
        String reason = inferred.isEmpty()
                ? "agent_policy:" + normalizeAgent(consumerAgent)
                : "query_intent:" + String.join(",", inferred);
        return new RetrievalPlan(List.copyOf(allowed), List.copyOf(preferred), reason);
    }

    private static List<String> inferQueryTypes(String query) {
        if (!StringUtils.hasText(query)) {
            return List.of();
        }
        String q = query.toLowerCase(Locale.ROOT);
        List<String> out = new ArrayList<>();
        if (containsAny(q, "候选人", "简历", "岗位", "jd", "技能", "经历", "偏好", "事实")) {
            out.add("SEMANTIC");
        }
        if (containsAny(q, "历史", "上次", "之前", "成功", "失败", "评估结果", "经验", "对比")) {
            out.add("EPISODIC");
        }
        if (containsAny(q, "规则", "流程", "策略", "评分标准", "skill", "技能说明", "怎么评")) {
            out.add("PROCEDURAL");
        }
        if (containsAny(q, "本次", "当前", "刚才", "草稿", "checkpoint", "上下文", "scratch")) {
            out.add("WORKING");
        }
        return out;
    }

    private static boolean containsAny(String text, String... needles) {
        for (String needle : needles) {
            if (text.contains(needle)) {
                return true;
            }
        }
        return false;
    }

    private static String normalizeAgent(String consumerAgent) {
        return StringUtils.hasText(consumerAgent)
                ? consumerAgent.trim().toUpperCase(Locale.ROOT)
                    .replace("-", "").replace("_", "")
                : "SPECIALIST";
    }

    public static String canonicalTaxonomy(String rawType) {
        if (!StringUtils.hasText(rawType)) {
            return "";
        }
        String normalized = rawType.trim().toUpperCase(Locale.ROOT);
        return LEGACY_TYPE_REMAP.getOrDefault(normalized, normalized);
    }

    private static boolean requestsLegacyFailure(List<String> requested) {
        return requested != null && requested.stream()
                .filter(StringUtils::hasText)
                .anyMatch(t -> "FAILURE".equalsIgnoreCase(t.trim()));
    }

    private static Set<String> storageTypesFor(List<String> canonicalTypes) {
        Set<String> out = new HashSet<>();
        for (String type : canonicalTypes) {
            out.addAll(STORAGE_TYPES.getOrDefault(type, Set.of(type)));
        }
        return out;
    }

    private static double typeAffinity(String taxonomy, List<String> preferred) {
        int index = preferred.indexOf(taxonomy);
        if (index == 0) {
            return 1.12;
        }
        if (index == 1) {
            return 1.04;
        }
        return 0.92;
    }

    private static double recencyHalfLifeDays(String taxonomy) {
        return switch (taxonomy) {
            case "WORKING" -> 2.0;
            case "SEMANTIC" -> 60.0;
            case "PROCEDURAL" -> 365.0;
            default -> 90.0;
        };
    }

    private boolean isVisibleInNamespace(MemoryEntryRow row, String taxonomy,
                                         SearchRequest request, boolean allowFailure) {
        String scope = row.getOwnerScope();
        if ("RUN".equals(scope)) {
            return "WORKING".equals(taxonomy)
                    && StringUtils.hasText(request.runId())
                    && request.runId().equals(row.getRunId());
        }
        if ("CONVERSATION".equals(scope)) {
            // Never widen candidate facts by userId. Conversation equality is
            // the candidate isolation boundary.
            return StringUtils.hasText(request.conversationId())
                    && request.conversationId().equals(row.getConversationId());
        }
        if ("USER".equals(scope)) {
            return StringUtils.hasText(request.userId())
                    && request.userId().equals(row.getUserId());
        }
        if ("GLOBAL".equals(scope)) {
            return "PROCEDURAL".equals(taxonomy)
                    || (allowFailure && isFailureEpisode(row));
        }
        return false;
    }

    private boolean isFailureEpisode(MemoryEntryRow row) {
        if (row == null) {
            return false;
        }
        if ("FAILURE".equalsIgnoreCase(row.getType())
                || "control_plane".equalsIgnoreCase(row.getSource())
                || "failed_run".equalsIgnoreCase(row.getSource())
                || (row.getSourceId() != null && row.getSourceId().startsWith("failure:"))) {
            return true;
        }
        Object structured = readJson(row.getStructuredContent());
        return structured instanceof Map<?, ?> map
                && ("FAILURE".equalsIgnoreCase(String.valueOf(map.get("outcome")))
                    || "CONTROL_PLANE".equalsIgnoreCase(String.valueOf(map.get("category"))));
    }

    private static boolean isApprovedProcedure(WriteRequest request) {
        String source = String.valueOf(request.source()).trim().toLowerCase(Locale.ROOT);
        if (TRUSTED_PROCEDURAL_SOURCES.contains(source)) {
            return true;
        }
        if (RUNTIME_STRATEGY_SOURCE.equals(source)) {
            return isValidatedRuntimeStrategy(
                    request.structuredContent(), request.runId(), request.ownerScope());
        }
        Map<String, Object> structured = request.structuredContent();
        if (structured == null) {
            return false;
        }
        return Boolean.TRUE.equals(structured.get("approved"))
                || "APPROVED".equalsIgnoreCase(String.valueOf(structured.get("approvalStatus")));
    }

    private boolean isApprovedProcedure(MemoryEntryRow row) {
        String source = String.valueOf(row.getSource()).trim().toLowerCase(Locale.ROOT);
        if (TRUSTED_PROCEDURAL_SOURCES.contains(source)) {
            return true;
        }
        Object structured = readJson(row.getStructuredContent());
        if (RUNTIME_STRATEGY_SOURCE.equals(source)) {
            return structured instanceof Map<?, ?> map
                    && isValidatedRuntimeStrategy(map, row.getRunId(), row.getOwnerScope());
        }
        return structured instanceof Map<?, ?> map
                && (Boolean.TRUE.equals(map.get("approved"))
                    || "APPROVED".equalsIgnoreCase(String.valueOf(map.get("approvalStatus"))));
    }

    /**
     * A runtime-learned procedure is accepted only when it is an attributable,
     * candidate-free observation from a real run.  It stays USER-scoped: the
     * strategy may be reused for that tenant's later resumes, while candidate
     * facts remain CONVERSATION-scoped and can never enter this namespace.
     */
    private static boolean isValidatedRuntimeStrategy(
            Map<?, ?> structured, String runId, String ownerScope) {
        if (structured == null
                || !"USER".equalsIgnoreCase(String.valueOf(ownerScope))
                || !StringUtils.hasText(runId)
                || !runId.equals(String.valueOf(structured.get("derivedFromRunId")))
                || !"execution_strategy".equals(structured.get("memoryKind"))
                || !Boolean.TRUE.equals(structured.get("actualExecution"))
                || !Boolean.TRUE.equals(structured.get("candidateDataExcluded"))) {
            return false;
        }
        Object selectedAgents = structured.get("selectedAgents");
        Object strategyClass = structured.get("strategyClass");
        return selectedAgents instanceof List<?> agents
                && !agents.isEmpty()
                && StringUtils.hasText(String.valueOf(strategyClass));
    }

    private String factKey(WriteRequest request) {
        if (request.structuredContent() != null
                && request.structuredContent().get("factKey") instanceof String key
                && StringUtils.hasText(key)) {
            return key;
        }
        return null;
    }

    /**
     * Runtime staging lives physically in WORKING/RUN rows, but it must not
     * share the ordinary WORKING dedup namespace. Durable stages additionally
     * keep distinct promotion destinations separate; otherwise equal content
     * intended for (for example) SEMANTIC/CONVERSATION and EPISODIC/USER could
     * collapse into one staging row and promote with the wrong metadata.
     */
    private static String runtimeDedupDiscriminator(WriteRequest request,
                                                    String normalizedSource) {
        Map<String, Object> structured = request.structuredContent();
        if (structured == null
                || !(structured.get(RUNTIME_WRITE_KEY) instanceof Map<?, ?> runtimeWrite)) {
            return "ordinary";
        }
        if (structured.get(PENDING_PROMOTION_KEY) instanceof Map<?, ?> promotion) {
            Object targetSourceId = promotion.get("sourceId");
            if (targetSourceId == null) {
                targetSourceId = structured.get("factKey");
            }
            Object targetSource = promotion.get("source");
            if (targetSource == null) {
                targetSource = normalizedSource;
            }
            return String.join("|",
                    "runtime-promotion",
                    dedupPart(promotion.get("type")),
                    dedupPart(promotion.get("ownerScope")),
                    dedupPart(targetSource),
                    dedupPart(targetSourceId));
        }
        /*
         * A runtime WORKING call must always get its own row. Including the
         * per-call writeId prevents the post-insert cancellation compensator
         * from ever receiving the memoryId of an older checkpoint/scratch row.
         */
        return "runtime-working|"
                + dedupPart(runtimeWrite.get(RUNTIME_WRITE_ID_KEY));
    }

    private static String dedupPart(Object value) {
        String text = value != null
                ? String.valueOf(value).trim().toLowerCase(Locale.ROOT) : "";
        return text.length() + ":" + text;
    }

    private static void addExactScope(QueryWrapper<MemoryEntryRow> query,
                                      WriteRequest request, String scope) {
        switch (scope) {
            case "RUN" -> query.eq("run_id", request.runId());
            case "CONVERSATION" -> query.eq("conversation_id", request.conversationId());
            case "USER" -> query.eq("user_id", request.userId());
            default -> {
                // GLOBAL has no owner key.
            }
        }
    }

    private static void validateScopeOwner(WriteRequest request, String scope) {
        if ("RUN".equals(scope) && !StringUtils.hasText(request.runId())) {
            throw new IllegalArgumentException("RUN scope requires runId");
        }
        if ("CONVERSATION".equals(scope)
                && !StringUtils.hasText(request.conversationId())) {
            throw new IllegalArgumentException("CONVERSATION scope requires conversationId");
        }
        if ("USER".equals(scope) && !StringUtils.hasText(request.userId())) {
            throw new IllegalArgumentException("USER scope requires userId");
        }
    }

    private String scopeKey(WriteRequest request, String scope) {
        return switch (scope) {
            case "RUN" -> String.valueOf(request.runId());
            case "CONVERSATION" -> String.valueOf(request.conversationId());
            case "USER" -> String.valueOf(request.userId());
            default -> "global";
        };
    }

    /** Opaque namespace label safe for traces (does not expose owner IDs). */
    public static String namespaceOf(MemoryEntryRow row) {
        if (row == null || !StringUtils.hasText(row.getOwnerScope())) {
            return "unknown";
        }
        String key = switch (row.getOwnerScope()) {
            case "RUN" -> row.getRunId();
            case "CONVERSATION" -> row.getConversationId();
            case "USER" -> row.getUserId();
            default -> "global";
        };
        if ("GLOBAL".equals(row.getOwnerScope())) {
            return "global";
        }
        return row.getOwnerScope().toLowerCase(Locale.ROOT) + "/"
                + sha256(String.valueOf(key)).substring(0, 12);
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
