package com.resumai.agent.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class AgentMemoryService {

    private final Path memoryRoot;
    private final Path episodicPath;
    private final Path semanticPath;
    private final Path proceduralPath;
    private final Path legacyPath;
    private final ObjectMapper objectMapper;

    public AgentMemoryService(@Value("${resumai.upload-dir:./uploads}") String uploadDir,
                              ObjectMapper objectMapper) {
        this.memoryRoot = Paths.get(uploadDir).toAbsolutePath().normalize().resolve("agent-memory");
        this.episodicPath = memoryRoot.resolve("episodic.json");
        this.semanticPath = memoryRoot.resolve("semantic.json");
        this.proceduralPath = memoryRoot.resolve("procedural.json");
        this.legacyPath = memoryRoot.resolve("memories.json");
        this.objectMapper = objectMapper;
        try {
            Files.createDirectories(memoryRoot);
            migrateLegacyIfNeeded();
        } catch (IOException e) {
            throw new IllegalStateException("无法创建 Agent memory 目录: " + memoryRoot, e);
        }
    }

    /**
     * File-based episodic/procedural memory for finished evaluations. The
     * unified run pipeline records the same signals through MemoryService
     * (memory_entry table); this JSON store remains for the analytics
     * overview endpoints.
     */
    public synchronized void recordRunOutcome(String traceId, String status,
                                              String recommendation, Integer overallScore,
                                              Long durationMs, String summary) {
        if (!StringUtils.hasText(traceId)) {
            return;
        }
        Map<String, Object> episodic = memoryRecord(
                "episodic",
                "run_result",
                traceId,
                tagsFor(status, recommendation, overallScore, summary),
                compact(summary, 1200),
                Map.of(
                        "status", valueOrEmpty(status),
                        "recommendation", valueOrEmpty(recommendation),
                        "score", overallScore == null ? 0 : overallScore,
                        "durationMs", durationMs == null ? 0 : durationMs),
                "future_routing_and_report_context",
                "Use as similar case only; never as candidate fact evidence.",
                importance(status, recommendation, overallScore, durationMs),
                0.86);
        prependAndTrim(episodicPath, episodic, 1000);

        for (Map<String, Object> procedural : deriveProceduralMemories(
                traceId, summary, durationMs, recommendation, overallScore)) {
            upsertMemory(proceduralPath, procedural, "recommendedAction", 200);
        }
    }

    public synchronized Map<String, Object> overview() {
        List<Map<String, Object>> episodic = load(episodicPath);
        List<Map<String, Object>> semantic = load(semanticPath);
        List<Map<String, Object>> procedural = load(proceduralPath);
        List<Map<String, Object>> all = new ArrayList<>();
        all.addAll(episodic);
        all.addAll(semantic);
        all.addAll(procedural);

        Map<String, Long> byType = new LinkedHashMap<>();
        byType.put("episodic", (long) episodic.size());
        byType.put("semantic", (long) semantic.size());
        byType.put("procedural", (long) procedural.size());

        List<Map<String, Object>> top = all.stream()
                .sorted(Comparator.comparingDouble(m -> -doubleValue(m.get("importance"))))
                .limit(10)
                .toList();

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("memoryArchitecture", List.of(
                "semantic_memory", "episodic_memory", "procedural_memory"));
        response.put("count", all.size());
        response.put("byType", byType);
        response.put("topMemories", top);
        response.put("layers", List.of(
                Map.of("type", "short_term", "storage", episodicPath.toString(), "count", episodic.size(),
                        "lifecycle", "TTL 7 days", "purpose", "最近评估案例，过期不参与路由"),
                Map.of("type", "long_term", "storage", semanticPath + " + " + proceduralPath,
                        "count", semantic.size() + procedural.size(),
                        "lifecycle", "no expiry", "purpose", "沉淀的评估规则与追问策略")));
        response.put("consolidationPolicy", Map.of(
                "episodicWrite", "every workflow result",
                "semanticWrite", "repeatable facts extracted from summary/risks/questions",
                "proceduralWrite", "routing/question/fallback rules from risk and latency patterns",
                "poisoningControl", "memory can influence strategy only, never candidate facts"));
        return response;
    }

    public synchronized Map<String, Object> search(String query, int topK) {
        List<String> terms = tokenize(query);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("query", query == null ? "" : query);
        result.put("episodicHits", searchLayer(load(episodicPath), terms, topK, "episodic"));
        result.put("semanticHits", searchLayer(load(semanticPath), terms, topK, "semantic"));
        result.put("proceduralHits", searchLayer(load(proceduralPath), terms, topK, "procedural"));
        result.put("poisoningControl", "Memory is strategy context only and must not be used as candidate factual evidence.");
        return result;
    }

    private List<Map<String, Object>> searchLayer(List<Map<String, Object>> rows, List<String> terms, int topK, String layer) {
        return rows.stream()
                .filter(row -> !isExpired(row))
                .map(row -> {
                    Map<String, Object> copy = new LinkedHashMap<>(row);
                    double score = score(searchText(row), terms);
                    copy.put("matchScore", score);
                    copy.put("matchReason", matchReason(row, terms, layer));
                    copy.put("lastUsedAt", LocalDateTime.now().toString());
                    copy.put("useCount", intValue(row.get("useCount")) + 1);
                    return copy;
                })
                .filter(row -> doubleValue(row.get("matchScore")) > 0)
                .sorted(Comparator.comparingDouble(row -> -doubleValue(row.get("matchScore"))))
                .limit(Math.max(topK, 1))
                .toList();
    }

    private List<Map<String, Object>> deriveProceduralMemories(String traceId, String summary,
                                                               Long durationMs,
                                                               String recommendation,
                                                               Integer overallScore) {
        String text = compact(summary, 1600);
        List<Map<String, Object>> records = new ArrayList<>();
        if (containsAny(text, "短简历", "信息较少", "证据不足")) {
            records.add(memoryRecord("procedural", "routing_policy", traceId,
                    List.of("sparse_resume", "fallback"),
                    "短简历必须启用 sparse evidence policy，减少长上下文 LLM，但报告必须显式说明证据不足。",
                    Map.of("trigger", "short_resume"), "route,report_context",
                    "Use sparse policy and require evidence-gap explanation.", 0.84, 0.86));
        }
        if (durationMs != null && durationMs > 45000) {
            records.add(memoryRecord("procedural", "latency_policy", traceId,
                    List.of("latency", "context_pack"),
                    "长 PDF 或慢任务应使用结构化摘要和定向 RAG，避免每个 Agent 重复读取完整简历。",
                    Map.of("durationMs", durationMs), "context_policy",
                    "Use context pack and dynamic query pruning.", 0.78, 0.8));
        }
        if ("NOT_RECOMMEND".equals(recommendation) || (overallScore != null && overallScore < 60)) {
            records.add(memoryRecord("procedural", "question_policy", traceId,
                    List.of("low_score", "interview_questions"),
                    "低分或不推荐候选人必须生成具体补充材料和验证型追问，不允许只给泛化问题。",
                    Map.of("score", overallScore == null ? 0 : overallScore), "questions",
                    "Generate concrete evidence-gap questions.", 0.82, 0.82));
        }
        return records;
    }

    private Map<String, Object> memoryRecord(String type,
                                             String scope,
                                             String traceId,
                                             List<String> tags,
                                             String content,
                                             Map<String, Object> evidence,
                                             String appliesTo,
                                             String recommendedAction,
                                             double importance,
                                             double confidence) {
        Map<String, Object> record = new LinkedHashMap<>();
        record.put("memoryId", "mem-" + UUID.randomUUID());
        record.put("type", type);
        record.put("scope", scope);
        record.put("traceId", traceId);
        record.put("tags", tags);
        record.put("content", compact(content, 1200));
        record.put("summary", compact(content, 800));
        record.put("evidence", evidence);
        record.put("appliesTo", appliesTo);
        record.put("recommendedAction", recommendedAction);
        record.put("importance", Math.min(Math.max(importance, 0), 1));
        record.put("confidence", Math.min(Math.max(confidence, 0), 1));
        record.put("createdAt", LocalDateTime.now().toString());
        record.put("lastUsedAt", null);
        record.put("useCount", 0);
        if ("episodic".equals(type)) {
            record.put("memoryScope", "short_term");
            record.put("expiresAt", LocalDateTime.now().plusDays(7).toString());
        } else {
            record.put("memoryScope", "long_term");
            record.put("expiresAt", null);
        }
        return record;
    }

    private List<String> tagsFor(String status, String recommendation,
                                 Integer overallScore, String summaryText) {
        List<String> tags = new ArrayList<>();
        tags.add("run");
        if (StringUtils.hasText(recommendation)) tags.add(recommendation.toLowerCase());
        if (overallScore != null && overallScore < 60) tags.add("low_score");
        String summary = summaryText == null ? "" : summaryText.toLowerCase();
        if (summary.contains("rag")) tags.add("rag");
        if (summary.contains("agent")) tags.add("agent");
        if (summary.contains("github")) tags.add("external_profile");
        return tags.stream().distinct().toList();
    }

    private double importance(String status, String recommendation,
                              Integer overallScore, Long durationMs) {
        double value = 0.4;
        if ("FAILED".equals(status)) value += 0.4;
        if ("NOT_RECOMMEND".equals(recommendation)) value += 0.2;
        if (overallScore != null && overallScore < 50) value += 0.2;
        if (durationMs != null && durationMs > 45000) value += 0.1;
        return Math.min(value, 1.0);
    }

    private void prependAndTrim(Path path, Map<String, Object> record, int limit) {
        List<Map<String, Object>> rows = load(path);
        rows.add(0, record);
        write(path, rows.stream().limit(limit).toList());
    }

    private void upsertMemory(Path path, Map<String, Object> record, String dedupeField, int limit) {
        List<Map<String, Object>> rows = load(path);
        String key = String.valueOf(record.getOrDefault(dedupeField, record.get("content"))).toLowerCase();
        boolean exists = false;
        for (Map<String, Object> row : rows) {
            String existing = String.valueOf(row.getOrDefault(dedupeField, row.get("content"))).toLowerCase();
            if (existing.equals(key)) {
                row.put("importance", Math.max(doubleValue(row.get("importance")), doubleValue(record.get("importance"))));
                row.put("confidence", Math.max(doubleValue(row.get("confidence")), doubleValue(record.get("confidence"))));
                row.put("lastUsedAt", LocalDateTime.now().toString());
                row.put("useCount", intValue(row.get("useCount")) + 1);
                exists = true;
                break;
            }
        }
        if (!exists) {
            rows.add(0, record);
        }
        write(path, rows.stream().limit(limit).toList());
    }

    private List<Map<String, Object>> load(Path path) {
        if (!Files.isRegularFile(path)) {
            return new ArrayList<>();
        }
        try {
            return objectMapper.readValue(path.toFile(), new TypeReference<>() {});
        } catch (Exception e) {
            return new ArrayList<>();
        }
    }

    private void write(Path path, List<Map<String, Object>> memories) {
        try {
            objectMapper.writerWithDefaultPrettyPrinter().writeValue(path.toFile(), memories);
        } catch (IOException e) {
            throw new IllegalStateException("write agent memory failed: " + e.getMessage(), e);
        }
    }

    private void migrateLegacyIfNeeded() throws IOException {
        if (Files.isRegularFile(episodicPath) || !Files.isRegularFile(legacyPath)) {
            return;
        }
        try {
            List<Map<String, Object>> legacy = objectMapper.readValue(legacyPath.toFile(), new TypeReference<>() {});
            List<Map<String, Object>> migrated = legacy.stream().map(row -> {
                Map<String, Object> copy = new LinkedHashMap<>(row);
                copy.putIfAbsent("content", row.getOrDefault("summary", ""));
                copy.putIfAbsent("tags", List.of("legacy"));
                copy.putIfAbsent("appliesTo", "future_routing_and_report_context");
                copy.putIfAbsent("recommendedAction", "Use as similar historical evaluation only.");
                copy.putIfAbsent("confidence", 0.65);
                copy.putIfAbsent("lastUsedAt", null);
                copy.putIfAbsent("useCount", 0);
                return copy;
            }).toList();
            write(episodicPath, migrated);
        } catch (Exception ignored) {
            Files.createFile(episodicPath);
        }
    }

    private String searchText(Map<String, Object> row) {
        return String.join(" ",
                String.valueOf(row.getOrDefault("content", "")),
                String.valueOf(row.getOrDefault("summary", "")),
                String.valueOf(row.getOrDefault("tags", "")),
                String.valueOf(row.getOrDefault("recommendedAction", "")),
                String.valueOf(row.getOrDefault("appliesTo", "")));
    }

    private String matchReason(Map<String, Object> row, List<String> terms, String layer) {
        String text = searchText(row).toLowerCase();
        List<String> matched = terms.stream().filter(text::contains).limit(5).toList();
        if (matched.isEmpty()) {
            return layer + " memory matched by fallback importance";
        }
        return layer + " memory matched terms: " + String.join(",", matched);
    }

    private String compact(String value, int max) {
        if (value == null) return "";
        return value.length() <= max ? value : value.substring(0, max);
    }

    private boolean isExpired(Map<String, Object> row) {
        Object expiresAt = row.get("expiresAt");
        if (expiresAt == null || !StringUtils.hasText(String.valueOf(expiresAt))) {
            return false;
        }
        try {
            return LocalDateTime.parse(String.valueOf(expiresAt)).isBefore(LocalDateTime.now());
        } catch (Exception e) {
            return false;
        }
    }

    private boolean containsAny(String text, String... needles) {
        if (!StringUtils.hasText(text)) return false;
        for (String needle : needles) {
            if (text.toLowerCase().contains(needle.toLowerCase())) {
                return true;
            }
        }
        return false;
    }

    private String valueOrEmpty(String value) {
        return value == null ? "" : value;
    }

    private List<String> tokenize(String text) {
        if (!StringUtils.hasText(text)) return List.of();
        List<String> terms = new ArrayList<>();
        for (String token : text.split("[\\s,，、/|；;:：()（）\\[\\]{}#]+")) {
            String trimmed = token.trim();
            if (trimmed.length() >= 2 && trimmed.length() <= 48) {
                terms.add(trimmed.toLowerCase());
            }
        }
        return terms.stream().distinct().toList();
    }

    private double score(String text, List<String> terms) {
        if (!StringUtils.hasText(text)) return 0;
        if (terms.isEmpty()) return 0.05;
        String lower = text.toLowerCase();
        long matched = terms.stream().filter(lower::contains).count();
        return (double) matched / Math.max(terms.size(), 1);
    }

    private double doubleValue(Object value) {
        return value instanceof Number n ? n.doubleValue() : 0D;
    }

    private int intValue(Object value) {
        return value instanceof Number n ? n.intValue() : 0;
    }
}
