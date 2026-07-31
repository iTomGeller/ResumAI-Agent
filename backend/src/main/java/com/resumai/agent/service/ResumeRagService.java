package com.resumai.agent.service;

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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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
    private final EmbeddingAvailability embeddingAvailability;
    private final MilvusVectorMaintenanceService vectorMaintenanceService;

    public ResumeRagService(@org.springframework.lang.Nullable MilvusEmbeddingStore embeddingStore,
                            EmbeddingModel embeddingModel,
                            EmbeddingAvailability embeddingAvailability,
                            MilvusVectorMaintenanceService vectorMaintenanceService) {
        this.embeddingStore = embeddingStore;
        this.embeddingModel = embeddingModel;
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

            List<String> verify = retrieveInternal(traceId, resumeText.substring(0, Math.min(200, resumeText.length())), 3);
            boolean verified = !verify.isEmpty();
            if (!verified) {
                log.warn("Read-after-write verification failed for traceId={}", traceId);
            }
            return new IndexResult(segments.size(), verified, verified ? null : "read_after_write_miss");
        } catch (Exception e) {
            String errorType = e.getClass().getSimpleName();
            if (e.getMessage() != null && e.getMessage().contains("collection not found")) {
                errorType = "CollectionNotFoundException";
            }
            log.warn("Milvus indexResume failed for traceId={}: {} - {}", traceId, errorType, e.getMessage());
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
        String query = StringUtils.hasText(jobQuery) ? jobQuery : "";
        String mode = StringUtils.hasText(strategy) ? strategy.trim().toLowerCase() : "hybrid";
        List<String> lexicalChunks = lexicalRetrieve(resumeText, query, topK);
        if ("hybrid".equals(mode)) {
            // Current-resume evidence must never search the process-wide
            // resume vector collection without a candidate scope.  The old
            // path called retrieveInternal(null, ...), which made dense hits
            // unverifiable and in production fell back 100% in the labelled
            // benchmark.  For a single short document, fuse an independent
            // section-aware channel with BM25-like lexical recall instead.
            List<String> structuralChunks = sectionAwareRetrieve(
                    resumeText, query, topK);
            List<String> chunks = rrfMergeChunks(
                    structuralChunks, lexicalChunks, topK);
            if (chunks.isEmpty()) {
                return withResumeTextFallback(
                        List.of(), 0, 0, "empty_scoped_resume_hits", true,
                        "current_resume", "section_bm25_rrf", null,
                        query, resumeText, topK);
            }
            return new RagRetrieveResult(
                    chunks, chunks.size(), 0, null, false,
                    "current_resume", "section_bm25_rrf", null,
                    query, false);
        }
        if ("lexical".equals(mode)) {
            if (!lexicalChunks.isEmpty()) {
                return new RagRetrieveResult(lexicalChunks, lexicalChunks.size(), 0.42, null, false,
                        "lexical", "bm25_like", null, query, false);
            }
            return withResumeTextFallback(List.of(), 0, 0, "empty_lexical_hits", true,
                    "lexical", "bm25_like", null, query, resumeText, topK);
        }
        if ("embedding".equals(mode)) {
            // A candidate-scoped dense index is not available on this API.
            // Fail closed to current-resume text instead of querying vectors
            // belonging to an unknown candidate.
            return withResumeTextFallback(
                    List.of(), 0, 0, "embedding_requires_candidate_scope", true,
                    "current_resume", "resume_text_fallback",
                    "candidate_scope_missing", query, resumeText, topK);
        }
        return withResumeTextFallback(
                List.of(), 0, 0, "unsupported_scoped_strategy:" + mode, true,
                "current_resume", "resume_text_fallback",
                "unsupported_strategy", query, resumeText, topK);
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

    private List<String> sectionAwareRetrieve(String resumeText, String query, int topK) {
        if (!StringUtils.hasText(resumeText)) {
            return List.of();
        }
        List<String> terms = tokenize(query);
        String queryLower = query == null ? "" : query.toLowerCase();
        List<ScoredChunk> scored = new ArrayList<>();
        for (String block : splitResumeBlocks(resumeText)) {
            String lower = block.toLowerCase();
            double score = 0;
            for (String term : terms) {
                if (lower.contains(term.toLowerCase())) {
                    score += 2.0;
                }
            }
            if (queryLower.contains("项目") && lower.contains("项目")) score += 4.0;
            if ((queryLower.contains("技术") || queryLower.contains("技能"))
                    && (lower.contains("技能") || lower.contains("熟练")
                    || lower.contains("掌握") || lower.contains("技术栈"))) {
                score += 3.0;
            }
            if ((queryLower.contains("工作") || queryLower.contains("经验"))
                    && (lower.contains("工作经历") || lower.contains("经验"))) {
                score += 2.0;
            }
            if (score > 0) {
                score += Math.min(block.length(), 600) / 1200.0;
                scored.add(new ScoredChunk(
                        block.length() > 600 ? block.substring(0, 600) : block,
                        score));
            }
        }
        scored.sort((a, b) -> Double.compare(b.score(), a.score()));
        return scored.stream().map(ScoredChunk::text).distinct()
                .limit(Math.max(1, topK)).toList();
    }

    private List<String> rrfMergeChunks(List<String> structuralChunks,
                                        List<String> lexicalChunks,
                                        int topK) {
        Map<String, Double> scores = new LinkedHashMap<>();
        for (int i = 0; i < structuralChunks.size(); i++) {
            scores.merge(structuralChunks.get(i), 0.5 / (61.0 + i), Double::sum);
        }
        for (int i = 0; i < lexicalChunks.size(); i++) {
            scores.merge(lexicalChunks.get(i), 0.5 / (61.0 + i), Double::sum);
        }
        return scores.entrySet().stream()
                .sorted((a, b) -> Double.compare(b.getValue(), a.getValue()))
                .map(Map.Entry::getKey)
                .limit(Math.max(1, topK))
                .toList();
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
        return chunks;
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
