package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.MemoryEntryMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Arrays;
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
 * Layered agent memory store (working / conversation / episodic / user
 * preference / HR feedback / domain / failure) with strict scope isolation,
 * lexical+recency+confidence retrieval, dedup by content hash, conflict
 * marking, expiry cleanup and sensitive-value redaction.
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

    private static final Map<String, String> LEGACY_TYPE_REMAP = Map.of(
            "USER_PREFERENCE", "PREFERENCE",
            "HR_FEEDBACK", "PREFERENCE",
            "WORKING", "CONVERSATION",
            "DOMAIN", "CONVERSATION");

    private static final Pattern SECRET_PATTERN = Pattern.compile(
            "(sk-[A-Za-z0-9]{16,}|Bearer\\s+[A-Za-z0-9._-]{16,}|password\\s*[:=]\\s*\\S+"
                    + "|api[_-]?key\\s*[:=]\\s*\\S+|AKID[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{12,})",
            Pattern.CASE_INSENSITIVE);

    private final MemoryEntryMapper memoryMapper;
    private final ObjectMapper objectMapper;
    private final MemoryVectorService vectorService;

    public MemoryService(MemoryEntryMapper memoryMapper, ObjectMapper objectMapper,
                         MemoryVectorService vectorService) {
        this.memoryMapper = memoryMapper;
        this.objectMapper = objectMapper;
        this.vectorService = vectorService;
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
        String content = redactSecrets(request.content());
        if (!StringUtils.hasText(content)) {
            throw new IllegalArgumentException("memory content required");
        }
        String hash = sha256(type + "|" + scope + "|" + scopeKey(request) + "|" + normalizeForHash(content));

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
        row.setSource(StringUtils.hasText(request.source()) ? request.source() : "model_generated");
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
                                Double minConfidence, Boolean includeSensitive) {
    }

    /**
     * Scope-isolated retrieval: an entry is visible only when its owner scope
     * chain matches the caller (run -> conversation -> user -> global).
     * Ranking = lexical overlap x confidence x recency decay.
     */
    public List<Map<String, Object>> search(SearchRequest request) {
        QueryWrapper<MemoryEntryRow> query = new QueryWrapper<MemoryEntryRow>()
                .eq("status", "ACTIVE")
                .and(w -> {
                    w.eq("owner_scope", "GLOBAL");
                    if (StringUtils.hasText(request.userId())) {
                        w.or(u -> u.eq("owner_scope", "USER").eq("user_id", request.userId()));
                    }
                    if (StringUtils.hasText(request.conversationId())) {
                        w.or(c -> c.eq("owner_scope", "CONVERSATION")
                                .eq("conversation_id", request.conversationId()));
                    }
                    if (StringUtils.hasText(request.runId())) {
                        w.or(r -> r.eq("owner_scope", "RUN").eq("run_id", request.runId()));
                    }
                });
        if (request.types() != null && !request.types().isEmpty()) {
            query.in("type", request.types());
        }
        query.orderByDesc("update_time").last("limit 400");
        List<MemoryEntryRow> candidates = memoryMapper.selectList(query);

        double minConfidence = request.minConfidence() != null ? request.minConfidence() : 0.0;
        boolean includeSensitive = Boolean.TRUE.equals(request.includeSensitive());
        Set<String> queryTerms = terms(request.query());
        // Two-way recall: semantic scores from Milvus are fused with the
        // lexical path; the DB-side scope/status/confidence filter above stays
        // authoritative so a vector hit can never cross scope boundaries.
        // Fusion weights pending EXP-5 (see harness/experiments).
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
            double lexical = overlap(queryTerms, terms(row.getContent()));
            double ageDays = Math.max(0,
                    Duration.between(row.getUpdateTime(), now).toHours() / 24.0);
            double recency = Math.exp(-ageDays / 30.0);
            double semantic = vectorScores.getOrDefault(row.getMemoryId(), 0.0);
            double relevance = Math.max(lexical, semantic);
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
            view.put("content", row.getContent());
            view.put("structuredContent", readJson(row.getStructuredContent()));
            view.put("source", row.getSource());
            view.put("sourceId", row.getSourceId());
            view.put("confidence", row.getConfidence());
            view.put("score", Math.round(entry.getValue() * 1000.0) / 1000.0);
            view.put("updatedAt", String.valueOf(row.getUpdateTime()));
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

    public void writeEpisodicRunMemory(AgentRun run, String terminalStatus) {
        Map<String, Object> structured = new LinkedHashMap<>();
        structured.put("runId", run.getRunId());
        structured.put("policyId", run.getPolicyId());
        structured.put("runType", run.getRunType());
        structured.put("status", terminalStatus);
        structured.put("promptVersions", readJson(run.getPromptVersions()));
        structured.put("skillVersions", readJson(run.getSkillVersions()));
        structured.put("metrics", readJson(run.getMetrics()));
        String content = "历史执行: 问题=" + trim(run.getUserMessage(), 200)
                + " | 策略=" + run.getPolicyId()
                + " | 类别=" + run.getRunType()
                + " | 结果=" + terminalStatus;
        write(new WriteRequest("EPISODIC", "CONVERSATION", run.getUserId(),
                run.getConversationId(), run.getRunId(), content, structured,
                "system_rule", "episodic:" + run.getRunId(), 0.8, "NORMAL", 90));
        archiveRunWorkingMemory(run.getRunId());
    }

    public void writeFailureMemory(AgentRun run, String errorCode, String errorMessage) {
        Map<String, Object> structured = new LinkedHashMap<>();
        structured.put("runId", run.getRunId());
        structured.put("errorCode", errorCode);
        structured.put("policyId", run.getPolicyId());
        structured.put("runType", run.getRunType());
        String content = "失败记录: 类别=" + run.getRunType() + " | 错误=" + errorCode
                + " | 详情=" + trim(errorMessage, 300);
        write(new WriteRequest("FAILURE", "GLOBAL", run.getUserId(), run.getConversationId(),
                run.getRunId(), content, structured, "system_rule",
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
        } catch (Exception e) {
            log.debug("memory cleanup skipped: {}", e.getMessage());
        }
    }

    // ------------------------------------------------------------------

    private String factKey(WriteRequest request) {
        if (request.structuredContent() != null
                && request.structuredContent().get("factKey") instanceof String key
                && StringUtils.hasText(key)) {
            return key;
        }
        return null;
    }

    private String scopeKey(WriteRequest request) {
        return String.join(":",
                String.valueOf(request.userId()),
                String.valueOf(request.conversationId()),
                "RUN".equals(request.ownerScope()) ? String.valueOf(request.runId()) : "-");
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
