package com.resumai.agent.service;

import com.resumai.agent.config.AgentMetrics;
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

/**
 * Milvus 向量检索服务 -- 对简历进行分块、嵌入、索引，并支持 ANN 检索。
 */
@Service
public class ResumeRagService {

    private static final Logger log = LoggerFactory.getLogger(ResumeRagService.class);
    private static final double SIMILARITY_THRESHOLD = 0.6;

    private final MilvusEmbeddingStore embeddingStore;
    private final EmbeddingModel embeddingModel;
    private final AgentMetrics agentMetrics;

    public ResumeRagService(MilvusEmbeddingStore embeddingStore, EmbeddingModel embeddingModel, AgentMetrics agentMetrics) {
        this.embeddingStore = embeddingStore;
        this.embeddingModel = embeddingModel;
        this.agentMetrics = agentMetrics;
    }

    public boolean isMilvusAvailable() {
        return embeddingStore != null;
    }

    public void indexResume(String traceId, String resumeText) {
        long start = System.currentTimeMillis();
        try {
            Document doc = Document.from(resumeText, Metadata.from("traceId", traceId));
            DocumentSplitter splitter = DocumentSplitters.recursive(500, 100);
            List<TextSegment> segments = splitter.split(doc);

            List<Embedding> embeddings = embeddingModel.embedAll(segments).content();
            embeddingStore.addAll(embeddings, segments);
            log.info("Indexed {} chunks for traceId={}", segments.size(), traceId);
            agentMetrics.recordMilvusChunksIndexed(segments.size());
            agentMetrics.recordToolCall("milvus_index", "HybridRagStrategy", "SUCCESS",
                    System.currentTimeMillis() - start);
        } catch (Exception e) {
            log.warn("Milvus indexResume failed for traceId={}: {}", traceId, e.getMessage());
            agentMetrics.recordMilvusChunksIndexed(0);
            agentMetrics.recordToolCallError("milvus_index", e.getClass().getSimpleName());
            agentMetrics.recordToolCall("milvus_index", "HybridRagStrategy", "FAILED",
                    System.currentTimeMillis() - start);
        }
    }

    public List<String> retrieve(String query, int topK) {
        long start = System.currentTimeMillis();
        try {
            Embedding queryEmbedding = embeddingModel.embed(query).content();
            EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                    .queryEmbedding(queryEmbedding)
                    .maxResults(topK)
                    .minScore(0.5)
                    .build();
            EmbeddingSearchResult<TextSegment> result = embeddingStore.search(request);
            List<EmbeddingMatch<TextSegment>> matches = result.matches();
            List<String> chunks = matches.stream()
                    .map(EmbeddingMatch::embedded)
                    .map(TextSegment::text)
                    .collect(Collectors.toList());

            agentMetrics.recordMilvusChunksRetrieved(chunks.size());
            for (EmbeddingMatch<TextSegment> match : matches) {
                agentMetrics.recordMilvusSimilarityScore(match.score());
            }
            if (chunks.isEmpty()) {
                agentMetrics.recordRagRetrievalEmptyResults();
            } else if (matches.stream().allMatch(match -> match.score() < SIMILARITY_THRESHOLD)) {
                agentMetrics.recordRagRetrievalBelowThreshold();
            }
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS",
                    System.currentTimeMillis() - start);
            return chunks;
        } catch (Exception e) {
            log.warn("Milvus retrieve failed: {}", e.getMessage());
            agentMetrics.recordMilvusChunksRetrieved(0);
            agentMetrics.recordRagRetrievalEmptyResults();
            agentMetrics.recordToolCallError("milvus_retrieve", e.getClass().getSimpleName());
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "FAILED",
                    System.currentTimeMillis() - start);
            return List.of();
        }
    }

    /**
     * Find previously indexed candidates that are similar to the given resume text.
     * Useful for comparative context: "Similar to candidates who scored 85+".
     */
    public List<SimilarCandidate> findSimilarCandidates(String resumeText, int topK) {
        try {
            String queryText = resumeText.length() > 1500 ? resumeText.substring(0, 1500) : resumeText;
            Embedding queryEmbedding = embeddingModel.embed(queryText).content();
            EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                    .queryEmbedding(queryEmbedding)
                    .maxResults(topK * 2)
                    .minScore(0.6)
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
}
