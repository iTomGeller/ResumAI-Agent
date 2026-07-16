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

    public ResumeRagService(@org.springframework.lang.Nullable MilvusEmbeddingStore embeddingStore,
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

    public record RagRetrieveResult(
            List<String> chunks,
            int hitCount,
            double topScore,
            String fallbackReason,
            boolean fallbackUsed,
            String backend,
            String strategy,
            String errorType,
            String query,
            boolean usedResumeTextFallback
    ) {}

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
        return retrieveDetailed(jobQuery, topK, resumeText, jdRequirements, "hybrid");
    }

    public RagRetrieveResult retrieveDetailed(String jobQuery, int topK, String resumeText, String jdRequirements, String strategy) {
        long start = System.currentTimeMillis();
        String query = StringUtils.hasText(jobQuery) ? jobQuery : "";
        String mode = StringUtils.hasText(strategy) ? strategy.trim().toLowerCase() : "hybrid";
        List<String> lexicalChunks = lexicalRetrieve(resumeText, query, topK);
        if ("hybrid".equals(mode) && shouldUseLexicalOnly(resumeText, lexicalChunks)) {
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS", System.currentTimeMillis() - start);
            return new RagRetrieveResult(lexicalChunks, lexicalChunks.size(), 0.42, null, false,
                    "hybrid", "lexical_short_resume", null, query, false);
        }
        if ("lexical".equals(mode)) {
            if (!lexicalChunks.isEmpty()) {
                agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS", System.currentTimeMillis() - start);
                return new RagRetrieveResult(lexicalChunks, lexicalChunks.size(), 0.42, null, false,
                        "lexical", "bm25_like", null, query, false);
            }
            return withResumeTextFallback(List.of(), 0, 0, "empty_lexical_hits", true,
                    "lexical", "bm25_like", null, query, resumeText, topK);
        }
        if (!embeddingAvailability.isOperational()) {
            if (!lexicalChunks.isEmpty()) {
                agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "WARNING", System.currentTimeMillis() - start);
                return new RagRetrieveResult(lexicalChunks, lexicalChunks.size(), 0.42,
                        embeddingAvailability.disabledReason(), false,
                        "hybrid", "lexical_only_embedding_disabled", embeddingAvailability.disabledReason(), query, false);
            }
            agentMetrics.recordRagRetrievalEmptyResults();
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "WARNING", System.currentTimeMillis() - start);
            return withResumeTextFallback(List.of(), 0, 0, embeddingAvailability.disabledReason(), true,
                    "hybrid", "disabled", embeddingAvailability.disabledReason(), query, resumeText, topK);
        }
        String compositeQuery = buildCompositeQuery(jobQuery, resumeText, jdRequirements);
        try {
            List<String> vectorChunks = "lexical".equals(mode) ? List.of() : retrieveInternal(null, compositeQuery, topK);
            List<String> chunks = "embedding".equals(mode)
                    ? vectorChunks
                    : mergeChunks(vectorChunks, lexicalChunks, topK);
            double topScore = !vectorChunks.isEmpty() ? 0.72 : (!lexicalChunks.isEmpty() ? 0.42 : 0);
            agentMetrics.recordMilvusChunksRetrieved(vectorChunks.size());
            agentMetrics.recordMilvusSimilarityScore(topScore);
            if (chunks.isEmpty()) {
                agentMetrics.recordRagRetrievalEmptyResults();
                agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS",
                        System.currentTimeMillis() - start);
                return withResumeTextFallback(
                        List.of(), 0, 0, "empty_" + mode + "_hits", true,
                        "hybrid", mode, null, query, resumeText, topK);
            }
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "SUCCESS",
                    System.currentTimeMillis() - start);
            String resolvedStrategy = "embedding".equals(mode) ? "embedding" : "hybrid_embedding_bm25";
            return new RagRetrieveResult(chunks, chunks.size(), topScore, null, false,
                    "hybrid", resolvedStrategy, null, query, false);
        } catch (Exception e) {
            String errorType = classifyEmbeddingError(e);
            log.warn("Milvus retrieve failed: {}", errorType);
            agentMetrics.recordMilvusChunksRetrieved(0);
            agentMetrics.recordRagRetrievalEmptyResults();
            agentMetrics.recordToolCallError("milvus_retrieve", errorType);
            agentMetrics.recordToolCall("milvus_retrieve", "HybridRagStrategy", "FAILED",
                    System.currentTimeMillis() - start);
            if (!lexicalChunks.isEmpty() && !"embedding".equals(mode)) {
                return new RagRetrieveResult(lexicalChunks, lexicalChunks.size(), 0.42, errorType, false,
                        "hybrid", "lexical_after_embedding_error", errorType, query, false);
            }
            return withResumeTextFallback(
                    List.of(), 0, 0, errorType, true,
                    "hybrid", mode, errorType, query, resumeText, topK);
        }
    }

    private String classifyEmbeddingError(Exception e) {
        String text = (e.getClass().getSimpleName() + " " + (e.getMessage() == null ? "" : e.getMessage())).toLowerCase();
        if (text.contains("modelnotfound") || text.contains("model not found")) {
            return "embedding_model_unavailable";
        }
        if (text.contains("401") || text.contains("unauthorized")) {
            return "embedding_api_unauthorized";
        }
        return e.getClass().getSimpleName();
    }

    private RagRetrieveResult withResumeTextFallback(
            List<String> chunks,
            int hitCount,
            double topScore,
            String fallbackReason,
            boolean fallbackUsed,
            String backend,
            String strategy,
            String errorType,
            String query,
            String resumeText,
            int topK) {
        if (!StringUtils.hasText(resumeText)) {
            return new RagRetrieveResult(chunks, hitCount, topScore, fallbackReason, fallbackUsed,
                    backend, strategy, errorType, query, false);
        }
        List<String> fallbackChunks = resumeTextFallbackChunks(resumeText, query, topK);
        if (fallbackChunks.isEmpty()) {
            return new RagRetrieveResult(chunks, hitCount, topScore, fallbackReason, fallbackUsed,
                    backend, strategy, errorType, query, false);
        }
        return new RagRetrieveResult(
                fallbackChunks,
                fallbackChunks.size(),
                topScore > 0 ? topScore : 0.35,
                fallbackReason != null ? fallbackReason + "+resume_text_fallback" : "resume_text_fallback",
                true,
                backend,
                "resume_text_fallback",
                errorType,
                query,
                true);
    }

    private List<String> resumeTextFallbackChunks(String resumeText, String query, int topK) {
        if (!StringUtils.hasText(resumeText)) {
            return List.of();
        }
        String[] keywords = query.split("[\\s,，、/|；;]+");
        List<String> paragraphs = new ArrayList<>();
        String[] blocks = resumeText.split("\\n{2,}");
        if (blocks.length <= 1) {
            for (String line : resumeText.split("\\n")) {
                if (StringUtils.hasText(line)) {
                    paragraphs.add(line.trim());
                }
            }
        } else {
            for (String block : blocks) {
                if (StringUtils.hasText(block)) {
                    paragraphs.add(block.trim());
                }
            }
        }
        List<String> scored = new ArrayList<>();
        for (String para : paragraphs) {
            int score = 0;
            for (String kw : keywords) {
                if (kw.length() >= 2 && para.toLowerCase().contains(kw.toLowerCase())) {
                    score++;
                }
            }
            if (score > 0 || !StringUtils.hasText(query)) {
                String snippet = para.length() > 500 ? para.substring(0, 500) : para;
                scored.add(snippet);
            }
        }
        if (scored.isEmpty() && StringUtils.hasText(query)) {
            String snippet = resumeText.length() > 500 ? resumeText.substring(0, 500) : resumeText;
            scored.add(snippet);
        }
        return scored.stream().limit(topK).toList();
    }

    private List<String> lexicalRetrieve(String resumeText, String query, int topK) {
        if (!StringUtils.hasText(resumeText)) {
            return List.of();
        }
        List<String> terms = tokenize(query);
        if (terms.isEmpty()) {
            terms = tokenize(extractSkillKeywords(resumeText));
        }
        List<String> docs = splitResumeBlocks(resumeText);
        List<ScoredChunk> scored = new ArrayList<>();
        int totalDocs = Math.max(docs.size(), 1);
        for (String doc : docs) {
            double score = 0;
            String lower = doc.toLowerCase();
            for (String term : terms) {
                long tf = countOccurrences(lower, term.toLowerCase());
                if (tf <= 0) {
                    continue;
                }
                long df = docs.stream().filter(d -> d.toLowerCase().contains(term.toLowerCase())).count();
                double idf = Math.log(1 + (totalDocs - df + 0.5) / (df + 0.5));
                score += (tf * 2.2 / (tf + 1.2)) * idf;
            }
            if (score > 0) {
                scored.add(new ScoredChunk(doc.length() > 600 ? doc.substring(0, 600) : doc, score));
            }
        }
        scored.sort((a, b) -> Double.compare(b.score(), a.score()));
        return scored.stream().map(ScoredChunk::text).distinct().limit(topK).toList();
    }

    private boolean shouldUseLexicalOnly(String resumeText, List<String> lexicalChunks) {
        if (!StringUtils.hasText(resumeText) || lexicalChunks.isEmpty()) {
            return false;
        }
        return resumeText.length() <= 800;
    }

    private List<String> splitResumeBlocks(String resumeText) {
        List<String> docs = new ArrayList<>();
        for (String block : resumeText.split("\\n{2,}")) {
            if (StringUtils.hasText(block)) {
                docs.add(block.trim());
            }
        }
        if (docs.size() <= 1) {
            docs.clear();
            for (String line : resumeText.split("\\n")) {
                if (StringUtils.hasText(line)) {
                    docs.add(line.trim());
                }
            }
        }
        if (docs.isEmpty() && StringUtils.hasText(resumeText)) {
            docs.add(resumeText.trim());
        }
        return docs;
    }

    private List<String> tokenize(String text) {
        if (!StringUtils.hasText(text)) {
            return List.of();
        }
        List<String> terms = new ArrayList<>();
        for (String token : text.split("[\\s,，、/|；;:：()（）\\[\\]{}]+")) {
            String trimmed = token.trim();
            if (trimmed.length() >= 2 && trimmed.length() <= 32 && !trimmed.matches("\\d+")) {
                terms.add(trimmed);
            }
        }
        return terms.stream().distinct().limit(30).toList();
    }

    private long countOccurrences(String text, String term) {
        if (!StringUtils.hasText(text) || !StringUtils.hasText(term)) {
            return 0;
        }
        long count = 0;
        int idx = 0;
        while ((idx = text.indexOf(term, idx)) >= 0) {
            count++;
            idx += Math.max(term.length(), 1);
        }
        return count;
    }

    private List<String> mergeChunks(List<String> vectorChunks, List<String> lexicalChunks, int topK) {
        List<String> merged = new ArrayList<>();
        for (String chunk : vectorChunks) {
            if (StringUtils.hasText(chunk) && !merged.contains(chunk)) {
                merged.add(chunk);
            }
        }
        for (String chunk : lexicalChunks) {
            if (StringUtils.hasText(chunk) && !merged.contains(chunk)) {
                merged.add(chunk);
            }
        }
        return merged.stream().limit(topK).toList();
    }

    private record ScoredChunk(String text, double score) {}

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
