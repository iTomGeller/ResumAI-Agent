package com.resumai.agent.service;

import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.rag.RagOptions;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.lang.Nullable;
import org.springframework.stereotype.Service;

@Service
public class HybridRagService {

    private static final Logger log = LoggerFactory.getLogger(HybridRagService.class);
    private static final ExecutorService COMPARE_POOL = Executors.newFixedThreadPool(4);
    private static final Pattern RANKED_IDS = Pattern.compile(
            "\"rankedIds\"\\s*:\\s*\\[([^\\]]*)\\]", Pattern.CASE_INSENSITIVE);

    private final JdRagService jdRagService;
    private final EmbeddingAvailability embeddingAvailability;
    private final DeepSeekClient deepSeekClient;

    public HybridRagService(JdRagService jdRagService,
                            EmbeddingAvailability embeddingAvailability,
                            @Nullable DeepSeekClient deepSeekClient) {
        this.jdRagService = jdRagService;
        this.embeddingAvailability = embeddingAvailability;
        this.deepSeekClient = deepSeekClient;
    }

    public List<JdMatchResult> retrieve(String resumeText, RagOptions opts) {
        RagOptions effective = opts != null ? opts : RagOptions.defaults();
        String retrievalQuery = deterministicAliasRewrite(resumeText);
        List<JdMatchResult> candidates = switch (effective.strategy()) {
            case "lexical" -> jdRagService.matchTopJdsViaLexical(retrievalQuery, effective.topK());
            case "vector" -> retrieveVector(retrievalQuery, effective);
            // The Neo4j graph strategy was removed with the knowledge graph
            // (F item): unknown/legacy "graph" requests degrade to hybrid.
            case "hybrid", "graph" -> hybridRRF(retrievalQuery, effective);
            default -> throw new IllegalArgumentException("Unknown strategy: " + effective.strategy());
        };
        if (effective.rerankerEnabled() && candidates.size() > 1) {
            List<JdMatchResult> reranked = llmRerank(resumeText, candidates);
            if (reranked != null && !reranked.isEmpty()) {
                return reranked.stream().limit(effective.topK()).toList();
            }
        }
        return candidates;
    }

    /** Held-out winner: deterministic aliases, with no hidden LLM rewrite. */
    private String deterministicAliasRewrite(String text) {
        if (text == null || text.isBlank()) return text == null ? "" : text;
        String rewritten = text;
        Map<String, String> aliases = new LinkedHashMap<>();
        aliases.put("springboot", "Spring Boot");
        aliases.put("spring-boot", "Spring Boot");
        aliases.put("k8s", "Kubernetes");
        aliases.put("大模型", "LLM");
        aliases.put("大型语言模型", "LLM");
        aliases.put("智能体", "Agent");
        aliases.put("检索增强生成", "RAG");
        aliases.put("向量数据库", "Milvus");
        aliases.put("消息队列", "MQ");
        aliases.put("关系型数据库", "SQL");
        aliases.put("持续集成", "CI");
        aliases.put("持续交付", "CD");
        for (Map.Entry<String, String> alias : aliases.entrySet()) {
            rewritten = rewritten.replaceAll(
                    "(?i)(?<![a-z0-9])" + Pattern.quote(alias.getKey())
                            + "(?![a-z0-9])",
                    Matcher.quoteReplacement(alias.getValue()));
        }
        return rewritten;
    }

    public Map<String, Object> compare(String resumeText, List<NamedVariant> variants) {
        Map<String, CompletableFuture<VariantResult>> futures = new LinkedHashMap<>();
        for (NamedVariant variant : variants) {
            futures.put(variant.name(), CompletableFuture.supplyAsync(
                    () -> runVariant(resumeText, variant), COMPARE_POOL));
        }
        Map<String, Object> results = new LinkedHashMap<>();
        futures.forEach((name, future) -> {
            try {
                VariantResult result = future.get();
                results.put(name, Map.of(
                        "candidates", result.candidates(),
                        "metricsMs", result.metricsMs(),
                        "strategy", result.strategy()));
            } catch (Exception e) {
                log.warn("Variant {} failed: {}", name, e.getMessage());
                results.put(name, Map.of("error", e.getMessage()));
            }
        });
        return results;
    }

    public Map<String, Object> preview(String resumeText, RagOptions opts) {
        long start = System.currentTimeMillis();
        List<JdMatchResult> candidates = retrieve(resumeText, opts);
        long elapsed = System.currentTimeMillis() - start;
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("candidates", candidates);
        body.put("metricsMs", elapsed);
        body.put("latencyMs", elapsed);
        body.put("strategy", opts != null ? opts.strategy() : "hybrid");
        body.put("rerankerEnabled", opts != null && opts.rerankerEnabled());
        body.put("retrievedAt", java.time.LocalDateTime.now().toString());
        body.put("scoreFields", List.of("matchScore", "retrievalScore", "vectorScore", "bm25Score", "rrfScore"));
        body.put("provenanceFields", List.of(
                "documentId", "chunkId", "version", "createdAt", "updatedAt", "charStart", "charEnd"));
        return body;
    }

    private VariantResult runVariant(String resumeText, NamedVariant variant) {
        long start = System.currentTimeMillis();
        List<JdMatchResult> candidates = retrieve(resumeText, variant.options());
        return new VariantResult(candidates, System.currentTimeMillis() - start, variant.options().strategy());
    }

    private List<JdMatchResult> retrieveVector(String resumeText, RagOptions opts) {
        if (!embeddingAvailability.isOperational()) {
            return jdRagService.matchTopJdsViaLexical(resumeText, opts.topK());
        }
        List<JdMatchResult> vector = jdRagService.matchTopJdsViaVector(resumeText, opts.topK(), opts);
        return vector.isEmpty()
                ? jdRagService.matchTopJdsViaLexical(resumeText, opts.topK())
                : vector;
    }

    private List<JdMatchResult> hybridRRF(String resumeText, RagOptions opts) {
        // Pull Top-20 (or more) before optional LLM rerank truncates to topK.
        int fuseLimit = opts.rerankerEnabled()
                ? Math.max(20, opts.topK())
                : opts.topK();
        int perChannelRecall = Math.min(200, Math.max(50, fuseLimit * 5));
        List<JdMatchResult> vectorHits = embeddingAvailability.isOperational()
                ? jdRagService.matchTopJdsViaVector(resumeText, perChannelRecall, opts)
                : List.of();
        List<JdMatchResult> lexicalHits = jdRagService.matchTopJdsViaLexical(resumeText, perChannelRecall);

        if (vectorHits.isEmpty() && lexicalHits.isEmpty()) {
            return List.of();
        }
        if (vectorHits.isEmpty()) {
            return lexicalHits.stream().limit(fuseLimit).toList();
        }
        if (lexicalHits.isEmpty()) {
            return vectorHits.stream().limit(fuseLimit).toList();
        }

        Map<String, Double> fusedScores = fuseRRFWeighted(vectorHits, lexicalHits, opts);
        Map<String, JdMatchResult> byId = new HashMap<>();
        Map<String, Double> vectorById = new HashMap<>();
        Map<String, Double> bm25ById = new HashMap<>();
        // 优先保留向量路结果的业务分/维度；词面路补充 bm25 召回分
        for (JdMatchResult r : vectorHits) {
            byId.putIfAbsent(r.jdId(), r);
            if (r.vectorScore() != null) {
                vectorById.put(r.jdId(), r.vectorScore());
            }
        }
        for (JdMatchResult r : lexicalHits) {
            byId.putIfAbsent(r.jdId(), r);
            if (r.bm25Score() != null) {
                bm25ById.put(r.jdId(), r.bm25Score());
            }
        }

        List<JdMatchResult> fused = new ArrayList<>();
        fusedScores.entrySet().stream()
                .sorted(Map.Entry.<String, Double>comparingByValue(Comparator.reverseOrder()))
                .limit(fuseLimit)
                .forEach(entry -> {
                    JdMatchResult base = byId.get(entry.getKey());
                    if (base != null) {
                        // RRF 只写入召回解释字段，不得覆盖业务 matchScore
                        fused.add(base.withRetrieval(
                                entry.getValue(),
                                vectorById.get(entry.getKey()),
                                bm25ById.get(entry.getKey())));
                    }
                });
        return fused;
    }

    /**
     * Listwise DeepSeek rerank over Top-N JD candidates. Returns null on any
     * failure so the caller keeps the RRF order.
     */
    private List<JdMatchResult> llmRerank(String resumeText, List<JdMatchResult> candidates) {
        if (deepSeekClient == null || candidates.isEmpty()) {
            return null;
        }
        List<JdMatchResult> top = candidates.stream().limit(20).toList();
        String snippet = resumeText == null ? ""
                : resumeText.substring(0, Math.min(resumeText.length(), 800));
        StringBuilder sb = new StringBuilder();
        sb.append("你是岗位匹配重排器。根据简历片段对候选 JD 按相关性降序重排，")
                .append("只输出 JSON：{\"rankedIds\":[\"jdId\",...]}\n简历片段:\n")
                .append(snippet)
                .append("\n候选:\n");
        for (int i = 0; i < top.size(); i++) {
            JdMatchResult c = top.get(i);
            sb.append(i + 1).append(". id=").append(c.jdId())
                    .append(" title=").append(c.title())
                    .append(" category=").append(c.category())
                    .append(" matchScore=").append(String.format(Locale.ROOT, "%.3f", c.matchScore()))
                    .append('\n');
        }
        try {
            String text = deepSeekClient.evaluateResume(sb.toString(), "JdReranker", "listwise_rerank");
            List<String> rankedIds = parseRankedIds(text);
            if (rankedIds.isEmpty()) {
                return null;
            }
            Map<String, JdMatchResult> byId = new LinkedHashMap<>();
            for (JdMatchResult c : top) {
                byId.put(c.jdId(), c);
            }
            List<JdMatchResult> ordered = new ArrayList<>();
            for (String id : rankedIds) {
                JdMatchResult row = byId.remove(id);
                if (row != null) {
                    ordered.add(row);
                }
            }
            ordered.addAll(byId.values());
            return ordered;
        } catch (Exception e) {
            log.debug("JD LLM rerank skipped: {}", e.getMessage());
            return null;
        }
    }

    private static List<String> parseRankedIds(String text) {
        if (text == null || text.isBlank()) {
            return List.of();
        }
        Matcher m = RANKED_IDS.matcher(text);
        if (!m.find()) {
            return List.of();
        }
        List<String> ids = new ArrayList<>();
        Matcher idm = Pattern.compile("\"([^\"]+)\"").matcher(m.group(1));
        while (idm.find()) {
            ids.add(idm.group(1));
        }
        return ids;
    }

    private Map<String, Double> fuseRRFWeighted(List<JdMatchResult> vectorHits,
                                                 List<JdMatchResult> lexicalHits,
                                                 RagOptions opts) {
        Map<String, Double> scores = new HashMap<>();
        for (int i = 0; i < vectorHits.size(); i++) {
            String id = vectorHits.get(i).jdId();
            double rrf = opts.semanticWeight() / (opts.rrfK() + i + 1);
            scores.merge(id, rrf, Double::sum);
        }
        for (int i = 0; i < lexicalHits.size(); i++) {
            String id = lexicalHits.get(i).jdId();
            double rrf = opts.keywordWeight() / (opts.rrfK() + i + 1);
            scores.merge(id, rrf, Double::sum);
        }
        return scores;
    }

    public record NamedVariant(String name, RagOptions options) {}

    private record VariantResult(List<JdMatchResult> candidates, long metricsMs, String strategy) {}
}
