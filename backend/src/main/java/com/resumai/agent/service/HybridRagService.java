package com.resumai.agent.service;

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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

@Service
public class HybridRagService {

    private static final Logger log = LoggerFactory.getLogger(HybridRagService.class);
    private static final ExecutorService COMPARE_POOL = Executors.newFixedThreadPool(4);

    private final JdRagService jdRagService;
    private final ResumeGraphService resumeGraphService;
    private final EmbeddingAvailability embeddingAvailability;

    public HybridRagService(JdRagService jdRagService,
                            ResumeGraphService resumeGraphService,
                            EmbeddingAvailability embeddingAvailability) {
        this.jdRagService = jdRagService;
        this.resumeGraphService = resumeGraphService;
        this.embeddingAvailability = embeddingAvailability;
    }

    public List<JdMatchResult> retrieve(String resumeText, RagOptions opts) {
        RagOptions effective = opts != null ? opts : RagOptions.defaults();
        return switch (effective.strategy()) {
            case "lexical" -> jdRagService.matchTopJdsViaLexical(resumeText, effective.topK());
            case "vector" -> retrieveVector(resumeText, effective);
            case "hybrid" -> hybridRRF(resumeText, effective);
            case "graph" -> resumeGraphService.matchViaGraph(resumeText, effective);
            default -> throw new IllegalArgumentException("Unknown strategy: " + effective.strategy());
        };
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
        return Map.of(
                "candidates", candidates,
                "metricsMs", elapsed,
                "strategy", opts != null ? opts.strategy() : "hybrid");
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
        int perChannelRecall = Math.min(200, Math.max(50, opts.topK() * 5));
        List<JdMatchResult> vectorHits = embeddingAvailability.isOperational()
                ? jdRagService.matchTopJdsViaVector(resumeText, perChannelRecall, opts)
                : List.of();
        List<JdMatchResult> lexicalHits = jdRagService.matchTopJdsViaLexical(resumeText, perChannelRecall);

        if (vectorHits.isEmpty() && lexicalHits.isEmpty()) {
            return List.of();
        }
        if (vectorHits.isEmpty()) {
            return lexicalHits.stream().limit(opts.topK()).toList();
        }
        if (lexicalHits.isEmpty()) {
            return vectorHits.stream().limit(opts.topK()).toList();
        }

        Map<String, Double> fusedScores = fuseRRFWeighted(vectorHits, lexicalHits, opts);
        Map<String, JdMatchResult> byId = new HashMap<>();
        vectorHits.forEach(r -> byId.putIfAbsent(r.jdId(), r));
        lexicalHits.forEach(r -> byId.putIfAbsent(r.jdId(), r));

        List<JdMatchResult> fused = new ArrayList<>();
        fusedScores.entrySet().stream()
                .sorted(Map.Entry.<String, Double>comparingByValue(Comparator.reverseOrder()))
                .limit(opts.topK())
                .forEach(entry -> {
                    JdMatchResult base = byId.get(entry.getKey());
                    if (base != null) {
                        fused.add(new JdMatchResult(
                                base.jdId(), base.title(), base.category(), entry.getValue(),
                                base.matchReasons(), base.gaps(), base.interviewChecks(),
                                base.skillMatchScore(), base.experienceMatchScore(),
                                base.projectMatchScore(), base.riskPenalty()));
                    }
                });
        return fused;
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
