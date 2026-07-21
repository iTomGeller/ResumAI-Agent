package com.resumai.agent.service;

import dev.langchain4j.data.document.Metadata;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingMatch;
import dev.langchain4j.store.embedding.EmbeddingSearchRequest;
import dev.langchain4j.store.embedding.EmbeddingSearchResult;
import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.lang.Nullable;
import org.springframework.stereotype.Service;

/**
 * Semantic recall source for long-term memory. Milvus only stores
 * (memoryId, vector); MySQL remains the source of truth and the scope /
 * confidence / status filter — vector hits that fail the DB-side scope check
 * are simply not returned. Every failure degrades to lexical-only recall.
 */
@Service
public class MemoryVectorService {

    private static final Logger log = LoggerFactory.getLogger(MemoryVectorService.class);

    private final MilvusEmbeddingStore store;
    private final EmbeddingModel embeddingModel;

    public MemoryVectorService(@Nullable @Qualifier("memoryEmbeddingStore") MilvusEmbeddingStore store,
                               @Nullable EmbeddingModel embeddingModel) {
        this.store = store;
        this.embeddingModel = embeddingModel;
    }

    public boolean available() {
        return store != null && embeddingModel != null;
    }

    /** Fire-and-forget index write; never blocks or fails the memory write. */
    public void indexAsync(String memoryId, String content) {
        if (!available() || memoryId == null || content == null || content.isBlank()) {
            return;
        }
        CompletableFuture.runAsync(() -> {
            try {
                Embedding embedding = embeddingModel.embed(content).content();
                TextSegment segment = TextSegment.from(
                        content.length() > 1000 ? content.substring(0, 1000) : content,
                        Metadata.from(Map.of("memoryId", memoryId)));
                store.add(embedding, segment);
            } catch (Exception e) {
                log.debug("memory vector index skipped id={}: {}", memoryId, e.getMessage());
            }
        });
    }

    /** memoryId -> cosine score for the query, empty on any failure. */
    public Map<String, Double> recall(String query, int topK) {
        if (!available() || query == null || query.isBlank()) {
            return Map.of();
        }
        try {
            Embedding embedding = embeddingModel.embed(query).content();
            EmbeddingSearchResult<TextSegment> result = store.search(
                    EmbeddingSearchRequest.builder()
                            .queryEmbedding(embedding)
                            .maxResults(Math.max(topK, 1))
                            .minScore(0.3)
                            .build());
            Map<String, Double> scores = new LinkedHashMap<>();
            for (EmbeddingMatch<TextSegment> match : result.matches()) {
                TextSegment segment = match.embedded();
                if (segment == null || segment.metadata() == null) {
                    continue;
                }
                String memoryId = segment.metadata().getString("memoryId");
                if (memoryId != null) {
                    scores.merge(memoryId, match.score(), Math::max);
                }
            }
            return scores;
        } catch (Exception e) {
            log.debug("memory vector recall degraded to lexical: {}", e.getMessage());
            return Map.of();
        }
    }
}
