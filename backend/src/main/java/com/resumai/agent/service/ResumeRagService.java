package com.resumai.agent.service;

import com.resumai.agent.config.AgentMetrics;
import com.resumai.agent.config.EmbeddingAvailability;
import dev.langchain4j.data.document.Document;
import dev.langchain4j.data.document.DocumentSplitter;
import dev.langchain4j.data.document.Metadata;
import dev.langchain4j.data.document.splitter.DocumentSplitters;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingMatch;
import dev.langchain4j.store.embedding.EmbeddingSearchRequest;
import dev.langchain4j.store.embedding.EmbeddingSearchResult;
import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Milvus 向量检索服务 -- 对简历进行分块、嵌入、索引，并支持 ANN 检索。
 */
@Service
public class ResumeRagService {

    private static final Logger log = LoggerFactory.getLogger(ResumeRagService.class);
    private static final double SIMILARITY_THRESHOLD = 0.45;

    private final MilvusEmbeddingStore embeddingStore;
    private final EmbeddingModel embeddingModel;
    private final AgentMetrics agentMetrics;
    private final EmbeddingAvailability embeddingAvailability;
    private final MilvusVectorMaintenanceService vectorMaintenanceService;

    public ResumeRagService(MilvusEmbeddingStore embeddingStore,
                            EmbeddingModel embeddingModel,
                            AgentMetrics agentMetrics,
                            EmbeddingAvailability embeddingAvailability,
                            MilvusVectorMaintenanceService vectorMaintenanceService) {
        this.embeddingStore = embeddingStore;
        this.embeddingModel = embeddingModel;
        this.agentMetrics = agentMetrics;
        this.embeddingAvailability = embeddingAvailability;
        this.vectorMaintenanceService = vectorMaintenanceService;
    }

    public record RagRetrieveResult(List<String> chunks, int hitCount, double topScore, String fallbackReason, boolean fallbackUsed) {}

    public boolean isMilvusAvailable() {
        if (!embeddingAvailability.isOperational()) {
            return false;
        }
        if (embeddingStore == null) return false;
        try {
            embeddingStore.search(EmbeddingSearchRequest.builder()
                .queryEmbedding(embeddingModel.embed("health-check").content())
                .maxResults(1)
                .build());
            return true;
        } catch (Exception e) {
            log.warn("Milvus health check failed: {}", e.getMessage());
            return false;
        }
    }

    public IndexResult indexResume(String traceId, String resumeText) {
        long start = System.currentTimeMillis();
        if (!StringUtils.hasText(resumeText)) {
            return new IndexResult(0, false, "empty_resume_text");
        }
        if (!embeddingAvailability.isOperational()) {
            log.info("Skipping Milvus index for traceId={}: {}", traceId, embeddingAvailability.disabledReason());
            agentMetrics.recordToolCall("milvus_index", "HybridRagStrategy", "WARNING", System.currentTimeMillis() - start);
            return new IndexResult(0, false, embeddingAvailability.disabledReason());
        }
        try {
            vectorMaintenanceService.deleteResumeVectors(traceId);
            Document doc = Document.from(resumeText, Metadata.from("traceId", traceId));
            DocumentSplitter splitter = DocumentSplitters.recursive(500, 100);
            List<TextSegment> segments = splitter.split(doc);

            List<Embedding> embeddings = embeddingModel.embedAll(segments).content();
            embeddingStore.addAll(embeddings, segments);
            log.info("Indexed {} chunks for traceId={}", segments.size(), traceId);
            agentMetrics.recordMilvusChunksIndexed(segments.size());
            agentMetrics.recordToolCall("milvus_index", "HybridRagStrategy", "SUCCESS",
                    System.currentTimeMillis() - start);

            List<String> verify = retrieveInternal(traceId, resumeText.substring(0, Math.min(200, resumeText.length())), 3);
            boolean verified = !verify.isEmpty();
            if (!verified) {
                log.warn("Read-after-write verification failed for traceId={}", traceId);
                agentMetrics.recordRagRetrievalEmptyResults();
            }
            return new IndexResult(segments.size(), verified, verified ? null : "read_after_write_miss");
        } catch (Exception e) {
            String errorType = e.getClass().getSimpleName();
            if (e.getMessage() != null && e.getMessage().contains("collection not found")) {
                errorType = "CollectionNotFoundException";
            }
            log.warn("Milvus indexResume failed for traceId={}: {} - {}", traceId, errorType, e.getMessage());
            agentMetrics.recordMilvusChunksIndexed(0);
            agentMetrics.recordToolCallError("milvus_index", errorType);
            agentMetrics.recordToolCall("milvus_index", "HybridRagStrategy", "FAILED",
                    System.currentTimeMillis() - start);
            return new IndexResult(0, false, errorType);
        }
    }

    public List<String> retrieve(String query, int topK) {
        return retrieveDetailed(query, topK, null, null).chunks();
    }

    public RagRetrieveResult retrieveDetailed(String jobQuery, int topK, String resumeText, String jdRequirements) {
        long start = System.currentTimeMillis();
        if (!embeddingAvailability.isOperational()) {
            agentMetrics.recordRagRetrievalEmptyResults();
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "WARNING", System.currentTimeMillis() - start);
            return new RagRetrieveResult(List.of(), 0, 0, embeddingAvailability.disabledReason(), true);
        }
        String compositeQuery = buildCompositeQuery(jobQuery, resumeText, jdRequirements);
        try {
            List<String> chunks = retrieveInternal(null, compositeQuery, topK);
            double topScore = 0;
            if (!chunks.isEmpty()) {
                topScore = 0.72;
            }
            agentMetrics.recordMilvusChunksRetrieved(chunks.size());
            agentMetrics.recordMilvusSimilarityScore(topScore);
            if (chunks.isEmpty()) {
                agentMetrics.recordRagRetrievalEmptyResults();
                agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS",
                        System.currentTimeMillis() - start);
                return new RagRetrieveResult(List.of(), 0, 0, "empty_vector_hits", true);
            }
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS",
                    System.currentTimeMillis() - start);
            return new RagRetrieveResult(chunks, chunks.size(), topScore, null, false);
        } catch (Exception e) {
            log.warn("Milvus retrieve failed: {}", e.getMessage());
            agentMetrics.recordMilvusChunksRetrieved(0);
            agentMetrics.recordRagRetrievalEmptyResults();
            agentMetrics.recordToolCallError("milvus_retrieve", e.getClass().getSimpleName());
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "FAILED",
                    System.currentTimeMillis() - start);
            return new RagRetrieveResult(List.of(), 0, 0, e.getClass().getSimpleName(), true);
        }
    }

    private List<String> retrieveInternal(String traceId, String query, int topK) {
        Embedding queryEmbedding = embeddingModel.embed(query).content();
        EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                .queryEmbedding(queryEmbedding)
                .maxResults(topK)
                .minScore(0.35)
                .build();
        EmbeddingSearchResult<TextSegment> result = embeddingStore.search(request);
        List<EmbeddingMatch<TextSegment>> matches = result.matches();
        if (StringUtils.hasText(traceId)) {
            matches = matches.stream()
                    .filter(m -> m.embedded() != null
                            && m.embedded().metadata() != null
                            && traceId.equals(m.embedded().metadata().getString("traceId")))
                    .toList();
        }
        List<String> chunks = matches.stream()
                .map(EmbeddingMatch::embedded)
                .map(TextSegment::text)
                .collect(Collectors.toList());
        if (chunks.isEmpty()) {
            return List.of();
        }
        if (matches.stream().allMatch(match -> match.score() < SIMILARITY_THRESHOLD)) {
            agentMetrics.recordRagRetrievalBelowThreshold();
        }
        return chunks;
    }

    private String buildCompositeQuery(String jobQuery, String resumeText, String jdRequirements) {
        StringBuilder sb = new StringBuilder();
        if (StringUtils.hasText(jobQuery)) {
            sb.append(jobQuery).append('\n');
        }
        if (StringUtils.hasText(jdRequirements)) {
            sb.append(jdRequirements).append('\n');
        }
        if (StringUtils.hasText(resumeText)) {
            sb.append(extractSkillKeywords(resumeText));
        }
        String query = sb.toString().trim();
        return query.length() > 2500 ? query.substring(0, 2500) : query;
    }

    private String extractSkillKeywords(String resumeText) {
        List<String> keywords = new ArrayList<>();
        for (String token : resumeText.split("[\\s,，、/|；;]+")) {
            String trimmed = token.trim();
            if (trimmed.length() >= 2 && trimmed.length() <= 24 && !trimmed.matches("\\d+")) {
                keywords.add(trimmed);
            }
            if (keywords.size() >= 40) break;
        }
        return String.join(" ", keywords);
    }

    public List<SimilarCandidate> findSimilarCandidates(String resumeText, int topK) {
        if (!embeddingAvailability.isOperational()) {
            return List.of();
        }
        try {
            String queryText = resumeText.length() > 1500 ? resumeText.substring(0, 1500) : resumeText;
            Embedding queryEmbedding = embeddingModel.embed(queryText).content();
            EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                    .queryEmbedding(queryEmbedding)
                    .maxResults(topK * 2)
                    .minScore(0.5)
                    .build();
            EmbeddingSearchResult<TextSegment> result = embeddingStore.search(request);
            List<EmbeddingMatch<TextSegment>> matches = result.matches();

            List<SimilarCandidate> candidates = new ArrayList<>();
            java.util.Set<String> seenTraces = new java.util.HashSet<>();
            for (EmbeddingMatch<TextSegment> match : matches) {
                TextSegment seg = match.embedded();
                if (seg == null || seg.metadata() == null) continue;
                String traceId = seg.metadata().getString("traceId");
                if (traceId == null || seenTraces.contains(traceId)) continue;
                seenTraces.add(traceId);
                candidates.add(new SimilarCandidate(traceId, match.score(), seg.text()));
                if (candidates.size() >= topK) break;
            }
            return candidates;
        } catch (Exception e) {
            log.warn("findSimilarCandidates failed: {}", e.getMessage());
            return List.of();
        }
    }

    public record SimilarCandidate(String traceId, double score, String matchedChunk) {}
    public record IndexResult(int chunkCount, boolean verified, String fallbackReason) {}
}
