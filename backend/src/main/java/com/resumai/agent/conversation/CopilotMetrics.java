package com.resumai.agent.conversation;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.atomic.LongAdder;
import org.springframework.stereotype.Component;

/**
 * Low-overhead in-process Copilot observability. The endpoint is intentionally
 * small and reset-on-restart; durable business data remains in MySQL.
 */
@Component
public class CopilotMetrics {

    private static final int SAMPLE_LIMIT = 1000;

    private final LongAdder requests = new LongAdder();
    private final LongAdder successfulRequests = new LongAdder();
    private final LongAdder providerFailures = new LongAdder();
    private final LongAdder contextCacheHits = new LongAdder();
    private final LongAdder contextCacheMisses = new LongAdder();
    private final LongAdder contextCacheRebuilds = new LongAdder();
    private final LongAdder providerCalls = new LongAdder();
    private final LongAdder promptTokens = new LongAdder();
    private final LongAdder cachedPromptTokens = new LongAdder();
    private final LongAdder providerUsageSamples = new LongAdder();

    private final ConcurrentLinkedDeque<Long> replyLatencyMs = new ConcurrentLinkedDeque<>();
    private final ConcurrentLinkedDeque<Long> asyncQueueLatencyMs = new ConcurrentLinkedDeque<>();
    private final ConcurrentLinkedDeque<Long> serverFirstDeltaLatencyMs = new ConcurrentLinkedDeque<>();
    private final ConcurrentLinkedDeque<Long> providerHeaderLatencyMs = new ConcurrentLinkedDeque<>();
    private final ConcurrentLinkedDeque<Long> providerFirstTokenLatencyMs = new ConcurrentLinkedDeque<>();
    private final ConcurrentLinkedDeque<Long> providerTotalLatencyMs = new ConcurrentLinkedDeque<>();
    private final Map<String, ConcurrentLinkedDeque<Long>> pipelineStagesMs =
            new ConcurrentHashMap<>();

    public void recordContextCacheHit() {
        contextCacheHits.increment();
    }

    public void recordContextCacheMiss() {
        contextCacheMisses.increment();
    }

    public void recordContextCacheRebuild() {
        contextCacheRebuilds.increment();
    }

    public void recordCopilotReply(boolean success, long elapsedMs) {
        requests.increment();
        if (success) {
            successfulRequests.increment();
        }
        addSample(replyLatencyMs, elapsedMs);
    }

    public void recordAsyncQueueLatency(long elapsedMs) {
        addSample(asyncQueueLatencyMs, elapsedMs);
    }

    public void recordServerFirstDeltaLatency(long elapsedMs) {
        addSample(serverFirstDeltaLatencyMs, elapsedMs);
    }

    public void recordPipelineStage(String stage, long elapsedMs) {
        if (stage == null || stage.isBlank()) {
            return;
        }
        addSample(pipelineStagesMs.computeIfAbsent(
                stage, ignored -> new ConcurrentLinkedDeque<>()), elapsedMs);
    }

    public void recordProviderFailure() {
        providerFailures.increment();
    }

    public void recordProviderCall(long headerMs, long firstTokenMs, long totalMs) {
        providerCalls.increment();
        addSample(providerHeaderLatencyMs, headerMs);
        addSample(providerFirstTokenLatencyMs, firstTokenMs);
        addSample(providerTotalLatencyMs, totalMs);
    }

    public void recordProviderUsage(Integer prompt, Integer cached) {
        if (prompt == null && cached == null) {
            return;
        }
        if (prompt != null && prompt > 0) {
            promptTokens.add(prompt);
        }
        if (cached != null && cached > 0) {
            cachedPromptTokens.add(cached);
        }
        providerUsageSamples.increment();
    }

    public Map<String, Object> snapshot() {
        long hits = contextCacheHits.sum();
        long misses = contextCacheMisses.sum();
        long lookups = hits + misses;
        long prompt = promptTokens.sum();
        long cached = cachedPromptTokens.sum();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("window", "process_since_restart");
        body.put("observedAt", Instant.now().toString());
        body.put("requests", requests.sum());
        body.put("successfulRequests", successfulRequests.sum());
        body.put("providerFailures", providerFailures.sum());
        Map<String, Object> contextCache = new LinkedHashMap<>();
        contextCache.put("hits", hits);
        contextCache.put("misses", misses);
        contextCache.put("lookups", lookups);
        contextCache.put("hitRate", lookups == 0 ? null : round(hits * 1.0 / lookups, 4));
        contextCache.put("rebuilds", contextCacheRebuilds.sum());
        contextCache.put("ttlSeconds", 7200);
        body.put("contextCache", contextCache);
        Map<String, Object> provider = new LinkedHashMap<>();
        provider.put("calls", providerCalls.sum());
        provider.put("headerLatencyMs", percentiles(providerHeaderLatencyMs));
        provider.put("firstTokenLatencyMs", percentiles(providerFirstTokenLatencyMs));
        provider.put("totalLatencyMs", percentiles(providerTotalLatencyMs));
        provider.put("promptTokensObserved", prompt);
        provider.put("cachedPromptTokensObserved", cached);
        provider.put("cachedTokenRate", prompt == 0 ? null : round(cached * 1.0 / prompt, 4));
        provider.put("usageSamples", providerUsageSamples.sum());
        body.put("provider", provider);
        body.put("replyLatencyMs", percentiles(replyLatencyMs));
        body.put("asyncQueueLatencyMs", percentiles(asyncQueueLatencyMs));
        body.put("serverFirstDeltaLatencyMs", percentiles(serverFirstDeltaLatencyMs));
        Map<String, Object> stages = new LinkedHashMap<>();
        pipelineStagesMs.keySet().stream().sorted().forEach(
                stage -> stages.put(stage, percentiles(pipelineStagesMs.get(stage))));
        body.put("pipelineStagesMs", stages);
        return body;
    }

    private static void addSample(ConcurrentLinkedDeque<Long> samples, long value) {
        samples.addLast(Math.max(0, value));
        while (samples.size() > SAMPLE_LIMIT) {
            samples.pollFirst();
        }
    }

    private static Map<String, Object> percentiles(ConcurrentLinkedDeque<Long> values) {
        List<Long> sorted = new ArrayList<>(values);
        Collections.sort(sorted);
        if (sorted.isEmpty()) {
            return Map.of("count", 0);
        }
        return Map.of(
                "count", sorted.size(),
                "p50", percentile(sorted, .50),
                "p95", percentile(sorted, .95),
                "max", sorted.get(sorted.size() - 1));
    }

    private static long percentile(List<Long> values, double p) {
        int index = (int) Math.ceil(values.size() * p) - 1;
        return values.get(Math.max(0, Math.min(index, values.size() - 1)));
    }

    private static double round(double value, int scale) {
        double factor = Math.pow(10, scale);
        return Math.round(value * factor) / factor;
    }
}
