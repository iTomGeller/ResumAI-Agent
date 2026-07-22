package com.resumai.agent.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.config.EmbeddingProperties;
import dev.langchain4j.data.document.Metadata;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingMatch;
import dev.langchain4j.store.embedding.EmbeddingSearchRequest;
import dev.langchain4j.store.embedding.EmbeddingSearchResult;
import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.lang.Nullable;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.multipart.MultipartFile;

/**
 * Self-service knowledge base: structure-aware chunking on disk + hybrid
 * BM25-like lexical / Milvus embedding retrieval with optional LLM rerank.
 */
@Service
public class KnowledgeBaseDocumentService {

    private static final Logger log = LoggerFactory.getLogger(KnowledgeBaseDocumentService.class);
    private static final int RRF_K = 60;
    public static final List<String> DOC_TYPE_ENUMS = List.of(
            "interview_rubric", "tech_guide", "policy", "verification_checklist",
            "question_bank", "scoring_standard", "general");
    public static final List<String> TAG_ENUMS = List.of(
            "java", "backend", "frontend", "python", "interview", "rubric",
            "risk", "project", "ats", "devops", "product", "ai");
    public static final List<String> FALLBACK_CHAIN = List.of("vector", "hybrid", "lexical", "none");
    public static final List<String> INDEX_STATUS_MACHINE = List.of(
            "pending", "indexing", "ready", "failed", "degraded");

    // EXP-2: grid-tunable via KB_CHUNK_CHARS / KB_OVERLAP_CHARS (default 320/60)
    private final int targetChunkChars;
    private final int overlapChars;
    private static final Pattern RANKED_IDS = Pattern.compile(
            "\"rankedIds\"\\s*:\\s*\\[([^\\]]*)\\]", Pattern.CASE_INSENSITIVE);

    private final Path kbRoot;
    private final Path manifestPath;
    private final ObjectMapper objectMapper;
    private final MilvusEmbeddingStore kbStore;
    private final EmbeddingModel embeddingModel;
    private final EmbeddingAvailability embeddingAvailability;
    private final EmbeddingProperties embeddingProperties;
    private final MilvusVectorMaintenanceService vectorMaintenance;
    private final DeepSeekClient deepSeekClient;

    public KnowledgeBaseDocumentService(@Value("${resumai.upload-dir:./uploads}") String uploadDir,
                                        @Value("${resumai.kb.chunk-chars:320}") int targetChunkChars,
                                        @Value("${resumai.kb.overlap-chars:60}") int overlapChars,
                                        ObjectMapper objectMapper,
                                        @Nullable @Qualifier("kbEmbeddingStore") MilvusEmbeddingStore kbStore,
                                        @Nullable EmbeddingModel embeddingModel,
                                        @Nullable EmbeddingAvailability embeddingAvailability,
                                        @Nullable EmbeddingProperties embeddingProperties,
                                        @Nullable MilvusVectorMaintenanceService vectorMaintenance,
                                        @Nullable DeepSeekClient deepSeekClient) {
        this.targetChunkChars = targetChunkChars > 0 ? targetChunkChars : 320;
        this.overlapChars = overlapChars >= 0 ? overlapChars : 60;
        this.kbRoot = Paths.get(uploadDir).toAbsolutePath().normalize().resolve("knowledge-base");
        this.manifestPath = kbRoot.resolve("manifest.json");
        this.objectMapper = objectMapper;
        this.kbStore = kbStore;
        this.embeddingModel = embeddingModel;
        this.embeddingAvailability = embeddingAvailability;
        this.embeddingProperties = embeddingProperties;
        this.vectorMaintenance = vectorMaintenance;
        this.deepSeekClient = deepSeekClient;
        try {
            Files.createDirectories(kbRoot);
        } catch (IOException e) {
            throw new IllegalStateException("无法创建知识库目录: " + kbRoot, e);
        }
    }

    public synchronized Map<String, Object> ingestText(String title, String content, String docType, String tags) {
        if (!StringUtils.hasText(content)) {
            throw new IllegalArgumentException("knowledge document content is empty");
        }
        return saveDocument(title, content, docType, tags, "text");
    }

    public synchronized Map<String, Object> ingestFile(MultipartFile file, String title, String docType, String tags) {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("knowledge document file is empty");
        }
        try {
            String name = file.getOriginalFilename() == null ? "knowledge-document" : file.getOriginalFilename();
            String lower = name.toLowerCase();
            String content;
            if (lower.endsWith(".pdf")) {
                try (PDDocument document = Loader.loadPDF(file.getBytes())) {
                    content = new PDFTextStripper().getText(document);
                }
            } else {
                content = new String(file.getBytes(), StandardCharsets.UTF_8);
            }
            return saveDocument(StringUtils.hasText(title) ? title : name, content, docType, tags,
                    lower.endsWith(".pdf") ? "pdf" : "text");
        } catch (IOException e) {
            throw new IllegalArgumentException("knowledge document parse failed: " + e.getMessage(), e);
        }
    }

    public synchronized Map<String, Object> overview() {
        List<Map<String, Object>> docs = loadManifest().stream()
                .map(this::enrichDocument)
                .toList();
        int chunkCount = docs.stream().mapToInt(d -> intValue(d.get("chunkCount"))).sum();
        Map<String, Long> byType = docs.stream()
                .collect(Collectors.groupingBy(d -> stringValue(d.get("docType"), "unknown"),
                        LinkedHashMap::new, Collectors.counting()));
        Map<String, Long> byIndexStatus = docs.stream()
                .collect(Collectors.groupingBy(d -> stringValue(d.get("indexStatus"), "pending"),
                        LinkedHashMap::new, Collectors.counting()));
        for (String status : INDEX_STATUS_MACHINE) {
            byIndexStatus.putIfAbsent(status, 0L);
        }
        boolean vectorReady = vectorReady();
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("documentCount", docs.size());
        response.put("chunkCount", chunkCount);
        response.put("chunkPolicy", Map.of(
                "targetChunkChars", targetChunkChars,
                "overlapChars", overlapChars,
                "splitPriority", List.of("markdown_heading", "numbered_section", "blank_line", "char_window")));
        response.put("retrievalPolicy", Map.of(
                "strategy", vectorReady ? "hybrid_bm25_embedding" : "lexical_bm25_like",
                "fusion", "rrf_k60",
                "rerank", "optional_deepseek_listwise",
                "vectorStore", vectorReady ? "milvus_kb_chunks" : "unavailable",
                "fallback", "vector→hybrid→lexical→none",
                "fallbackChain", FALLBACK_CHAIN));
        response.put("docTypes", byType);
        response.put("docTypeEnums", DOC_TYPE_ENUMS);
        response.put("tagEnums", TAG_ENUMS);
        response.put("indexStatusMachine", INDEX_STATUS_MACHINE);
        response.put("indexStatusCounts", byIndexStatus);
        response.put("fallbackChain", FALLBACK_CHAIN);
        response.put("embeddingProvider", embeddingProvider());
        response.put("indexVersion", indexVersion());
        response.put("documents", docs.stream().limit(100).toList());
        response.put("sampleChunks", sampleChunks(docs, 8));
        return response;
    }

    public synchronized List<Map<String, Object>> listDocuments() {
        return loadManifest().stream().map(this::enrichDocument).toList();
    }

    public synchronized Map<String, Object> getDocument(String docId) {
        for (Map<String, Object> doc : loadManifest()) {
            if (docId.equals(stringValue(doc.get("docId"), ""))) {
                Map<String, Object> result = enrichDocument(doc);
                Path txt = kbRoot.resolve(docId + ".txt");
                if (Files.isRegularFile(txt)) {
                    try {
                        result.put("content", Files.readString(txt, StandardCharsets.UTF_8));
                    } catch (IOException e) {
                        result.put("content", "");
                    }
                }
                Path chunksPath = kbRoot.resolve(docId + ".chunks.json");
                if (Files.isRegularFile(chunksPath)) {
                    try {
                        List<Map<String, Object>> chunks = objectMapper.readValue(
                                chunksPath.toFile(), new TypeReference<>() {});
                        String indexStatus = stringValue(result.get("indexStatus"), "pending");
                        result.put("chunks", chunks.stream()
                                .map(c -> enrichChunk(c, indexStatus, null))
                                .toList());
                    } catch (Exception e) {
                        result.put("chunks", List.of());
                    }
                }
                return result;
            }
        }
        Map<String, Object> notFound = new LinkedHashMap<>();
        notFound.put("error", "document not found");
        notFound.put("docId", docId);
        return notFound;
    }

    public synchronized boolean deleteDocument(String docId) {
        List<Map<String, Object>> manifest = loadManifest();
        boolean removed = manifest.removeIf(doc -> docId.equals(stringValue(doc.get("docId"), "")));
        if (removed) {
            try {
                objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), manifest);
                Files.deleteIfExists(kbRoot.resolve(docId + ".txt"));
                Files.deleteIfExists(kbRoot.resolve(docId + ".chunks.json"));
                if (vectorMaintenance != null) {
                    vectorMaintenance.deleteKbVectors(docId);
                }
            } catch (IOException e) {
                throw new IllegalStateException("delete knowledge document failed: " + e.getMessage(), e);
            }
        }
        return removed;
    }

    /** Backward-compatible: return only fused chunk list. */
    public List<Map<String, Object>> search(String query, int topK) {
        return searchDetailed(query, topK, false).chunks();
    }

    /** Search must not share the ingest/reindex monitor — vector index can take minutes. */
    public SearchResult searchDetailed(String query, int topK, boolean rerank) {
        int limit = Math.max(topK, 1);
        List<Map<String, Object>> lexical = lexicalSearch(query, Math.max(limit * 4, 20));
        List<Map<String, Object>> vector = vectorSearch(query, Math.max(limit * 4, 20));

        String strategy;
        String fallbackStage;
        List<Map<String, Object>> fused;
        if (!lexical.isEmpty() && !vector.isEmpty()) {
            fused = fuseRrf(lexical, vector, Math.max(limit * 2, 10));
            strategy = "hybrid_bm25_embedding";
            fallbackStage = "hybrid";
        } else if (!vector.isEmpty()) {
            fused = vector.stream().limit(Math.max(limit * 2, 10)).toList();
            strategy = "embedding_only";
            fallbackStage = "vector";
        } else if (!lexical.isEmpty()) {
            fused = lexical.stream().limit(Math.max(limit * 2, 10)).toList();
            strategy = vectorReady() ? "lexical_only" : "lexical_bm25_like";
            fallbackStage = "lexical";
        } else {
            fused = List.of();
            strategy = "none";
            fallbackStage = "none";
        }

        boolean rerankApplied = false;
        if (rerank && fused.size() > 1) {
            List<Map<String, Object>> reranked = llmRerank(query, fused);
            if (reranked != null && !reranked.isEmpty()) {
                fused = reranked;
                rerankApplied = true;
                strategy = strategy + "+llm_rerank";
            }
        }

        final String resolvedStrategy = strategy;
        final String resolvedStage = fallbackStage;
        final String queryText = query == null ? "" : query;
        List<Map<String, Object>> hits = fused.stream().limit(limit).map(row -> {
            Map<String, Object> copy = enrichChunk(row, null, resolvedStage);
            copy.put("strategy", resolvedStrategy);
            copy.put("query", queryText);
            copy.put("fallbackStage", resolvedStage);
            copy.put("fallbackUsed", "lexical".equals(resolvedStage) || "none".equals(resolvedStage));
            copy.put("enabled", true);
            return copy;
        }).toList();
        markHits(hits);
        return new SearchResult(hits, resolvedStrategy, lexical.size(), vector.size(),
                !lexical.isEmpty() && !vector.isEmpty() ? "rrf_k60" : "none",
                rerankApplied, resolvedStage, FALLBACK_CHAIN);
    }

    /**
     * Kick off a full rebuild asynchronously so nginx/proxy do not 504.
     * Poll document embeddingStatus / search strategy for completion.
     */
    public synchronized Map<String, Object> reindexAll() {
        if (!vectorReady()) {
            return Map.of(
                    "indexedChunks", 0,
                    "failedChunks", 0,
                    "documents", 0,
                    "status", "skipped",
                    "reason", embeddingAvailability != null
                            ? embeddingAvailability.statusMessage()
                            : "embedding_or_store_unavailable");
        }
        List<Map<String, Object>> manifest = loadManifest();
        for (Map<String, Object> doc : manifest) {
            doc.put("embeddingStatus", "reindexing");
            doc.put("indexStatus", "indexing");
        }
        try {
            objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), manifest);
        } catch (IOException e) {
            log.warn("KB manifest reindex mark failed: {}", e.getMessage());
        }
        java.util.concurrent.CompletableFuture.runAsync(this::reindexAllSync);
        return Map.of(
                "documents", manifest.size(),
                "status", "accepted",
                "strategy", "hybrid_bm25_embedding");
    }

    private void reindexAllSync() {
        int indexed = 0;
        int failed = 0;
        int docs = 0;
        if (vectorMaintenance != null) {
            vectorMaintenance.clearKbCollection();
        }
        List<Map<String, Object>> manifest;
        synchronized (this) {
            manifest = loadManifest();
        }
        for (Map<String, Object> doc : manifest) {
            docs++;
            String docId = stringValue(doc.get("docId"), "");
            Path chunksPath = kbRoot.resolve(docId + ".chunks.json");
            if (!Files.isRegularFile(chunksPath)) {
                failed++;
                doc.put("embeddingStatus", "failed");
                doc.put("indexStatus", "failed");
                continue;
            }
            try {
                List<Map<String, Object>> chunks = objectMapper.readValue(
                        chunksPath.toFile(), new TypeReference<>() {});
                int n = indexChunksSync(chunks);
                indexed += n;
                String embStatus = n > 0 ? "indexed" : "lexical_only";
                String indexStatus = n > 0 ? "ready" : (vectorReady() ? "degraded" : "degraded");
                doc.put("embeddingStatus", embStatus);
                doc.put("indexStatus", indexStatus);
                doc.put("embeddingProvider", embeddingProvider());
                doc.put("indexVersion", indexVersion());
                doc.put("updatedAt", LocalDateTime.now().toString());
                if (n == 0) {
                    failed++;
                }
            } catch (Exception e) {
                failed++;
                log.warn("KB reindex failed for {}: {}", docId, e.getMessage());
                doc.put("embeddingStatus", "failed");
                doc.put("indexStatus", "failed");
            }
        }
        synchronized (this) {
            try {
                objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), manifest);
            } catch (IOException e) {
                log.warn("KB manifest status update failed: {}", e.getMessage());
            }
        }
        log.info("KB reindex done: docs={} indexedChunks={} failed={}", docs, indexed, failed);
    }

    private Map<String, Object> saveDocument(String title, String content, String docType, String tags,
                                             String sourceFormat) {
        String docId = "kb-" + UUID.randomUUID();
        List<Map<String, Object>> chunks = chunkDocument(docId, title, content, docType, tags);
        boolean canIndex = vectorReady() && !chunks.isEmpty();
        String embStatus = canIndex ? "reindexing" : (vectorReady() ? "pending" : "lexical_only");
        String indexStatus = canIndex ? "indexing" : (vectorReady() ? "pending" : "degraded");
        for (Map<String, Object> chunk : chunks) {
            Object meta = chunk.get("metadata");
            if (meta instanceof Map<?, ?> m) {
                @SuppressWarnings("unchecked")
                Map<String, Object> mutable = new LinkedHashMap<>((Map<String, Object>) m);
                mutable.put("embeddingStatus", embStatus);
                mutable.put("indexStatus", indexStatus);
                mutable.put("embeddingProvider", embeddingProvider());
                mutable.put("indexVersion", indexVersion());
                chunk.put("metadata", mutable);
            }
        }
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("docId", docId);
        doc.put("title", StringUtils.hasText(title) ? title : docId);
        doc.put("docType", StringUtils.hasText(docType) ? docType : "general");
        doc.put("tags", splitTags(tags));
        doc.put("sourceFormat", sourceFormat);
        doc.put("charLength", content.length());
        doc.put("chunkCount", chunks.size());
        doc.put("embeddingStatus", embStatus);
        doc.put("indexStatus", indexStatus);
        doc.put("embeddingProvider", embeddingProvider());
        doc.put("indexVersion", indexVersion());
        doc.put("chunkPolicy", Map.of("targetChunkChars", targetChunkChars, "overlapChars", overlapChars));
        doc.put("usageCount", 0);
        doc.put("lastHitAt", null);
        doc.put("createdAt", LocalDateTime.now().toString());
        doc.put("updatedAt", LocalDateTime.now().toString());
        try {
            Files.writeString(kbRoot.resolve(docId + ".txt"), content, StandardCharsets.UTF_8);
            objectMapper.writerWithDefaultPrettyPrinter().writeValue(
                    kbRoot.resolve(docId + ".chunks.json").toFile(), chunks);
            List<Map<String, Object>> manifest = loadManifest();
            manifest.add(0, doc);
            objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), manifest);
        } catch (IOException e) {
            throw new IllegalStateException("save knowledge document failed: " + e.getMessage(), e);
        }
        if (canIndex) {
            scheduleIndex(docId, chunks);
        }
        return enrichDocument(doc);
    }

    private void scheduleIndex(String docId, List<Map<String, Object>> chunks) {
        List<Map<String, Object>> snapshot = List.copyOf(chunks);
        java.util.concurrent.CompletableFuture.runAsync(() -> {
            try {
                int n = indexChunksSync(snapshot);
                updateDocumentIndexStatus(docId, n > 0 ? "ready" : "degraded",
                        n > 0 ? "indexed" : "lexical_only");
            } catch (Exception e) {
                log.warn("KB async vector index failed: {}", e.getMessage());
                updateDocumentIndexStatus(docId, "failed", "failed");
            }
        });
    }

    private synchronized void updateDocumentIndexStatus(String docId, String indexStatus, String embStatus) {
        List<Map<String, Object>> manifest = loadManifest();
        boolean changed = false;
        for (Map<String, Object> doc : manifest) {
            if (docId.equals(stringValue(doc.get("docId"), ""))) {
                doc.put("indexStatus", indexStatus);
                doc.put("embeddingStatus", embStatus);
                doc.put("embeddingProvider", embeddingProvider());
                doc.put("indexVersion", indexVersion());
                doc.put("updatedAt", LocalDateTime.now().toString());
                changed = true;
                break;
            }
        }
        if (changed) {
            try {
                objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), manifest);
            } catch (IOException e) {
                log.warn("KB index status update failed: {}", e.getMessage());
            }
        }
    }

    private int indexChunksSync(List<Map<String, Object>> chunks) {
        if (!vectorReady() || chunks == null || chunks.isEmpty()) {
            return 0;
        }
        try {
            List<TextSegment> segments = new ArrayList<>();
            for (Map<String, Object> chunk : chunks) {
                String content = stringValue(chunk.get("content"), "");
                if (!StringUtils.hasText(content)) continue;
                Map<String, String> meta = new LinkedHashMap<>();
                meta.put("chunkId", stringValue(chunk.get("chunkId"), ""));
                meta.put("docId", stringValue(chunk.get("docId"), ""));
                meta.put("title", stringValue(chunk.get("title"), ""));
                meta.put("docType", stringValue(chunk.get("docType"), "general"));
                meta.put("sectionPath", stringValue(chunk.get("sectionPath"), ""));
                segments.add(TextSegment.from(content, Metadata.from(meta)));
            }
            if (segments.isEmpty()) return 0;
            // Small batches: remote providers rate-limit / timeout large embedAll calls.
            final int batchSize = 8;
            int stored = 0;
            for (int i = 0; i < segments.size(); i += batchSize) {
                List<TextSegment> batch = segments.subList(i, Math.min(i + batchSize, segments.size()));
                List<Embedding> embeddings = embeddingModel.embedAll(batch).content();
                kbStore.addAll(embeddings, batch);
                stored += batch.size();
            }
            return stored;
        } catch (Exception e) {
            log.warn("KB vector index failed: {}", e.getMessage());
            return 0;
        }
    }

    private List<Map<String, Object>> lexicalSearch(String query, int topN) {
        List<String> terms = tokenize(query);
        List<Map<String, Object>> scored = new ArrayList<>();
        for (Map<String, Object> doc : loadManifest()) {
            String docId = stringValue(doc.get("docId"), "");
            Path chunksPath = kbRoot.resolve(docId + ".chunks.json");
            if (!Files.isRegularFile(chunksPath)) continue;
            try {
                List<Map<String, Object>> chunks = objectMapper.readValue(
                        chunksPath.toFile(), new TypeReference<>() {});
                for (Map<String, Object> chunk : chunks) {
                    double score = scoreChunk(doc, chunk, terms);
                    if (score >= 0.12) {
                        Map<String, Object> row = new LinkedHashMap<>(chunk);
                        row.put("score", score);
                        row.put("channel", "lexical");
                        row.put("rerankReason", rerankReason(doc, chunk, terms));
                        row.put("topScore", score);
                        scored.add(row);
                    }
                }
            } catch (Exception ignored) {
            }
        }
        scored.sort((a, b) -> Double.compare(doubleValue(b.get("score")), doubleValue(a.get("score"))));
        return scored.stream().limit(Math.max(topN, 1)).toList();
    }

    private List<Map<String, Object>> vectorSearch(String query, int topN) {
        if (!vectorReady() || !StringUtils.hasText(query)) {
            return List.of();
        }
        try {
            Embedding embedding = embeddingModel.embed(query.length() > 1500
                    ? query.substring(0, 1500) : query).content();
            EmbeddingSearchResult<TextSegment> result = kbStore.search(
                    EmbeddingSearchRequest.builder()
                            .queryEmbedding(embedding)
                            .maxResults(Math.max(topN, 1))
                            .minScore(0.30)
                            .build());
            // Build a lookup of on-disk chunks for full payload.
            Map<String, Map<String, Object>> byChunkId = loadAllChunksById();
            List<Map<String, Object>> hits = new ArrayList<>();
            for (EmbeddingMatch<TextSegment> match : result.matches()) {
                TextSegment seg = match.embedded();
                if (seg == null || seg.metadata() == null) continue;
                String chunkId = seg.metadata().getString("chunkId");
                Map<String, Object> base = byChunkId.getOrDefault(chunkId, new LinkedHashMap<>());
                Map<String, Object> row = new LinkedHashMap<>(base);
                if (row.isEmpty()) {
                    row.put("chunkId", chunkId);
                    row.put("docId", seg.metadata().getString("docId"));
                    row.put("title", seg.metadata().getString("title"));
                    row.put("docType", seg.metadata().getString("docType"));
                    row.put("sectionPath", seg.metadata().getString("sectionPath"));
                    row.put("content", seg.text());
                    row.put("contentPreview", preview(seg.text(), 220));
                }
                row.put("score", match.score());
                row.put("channel", "vector");
                row.put("topScore", match.score());
                row.put("rerankReason", "vector cosine " + String.format("%.3f", match.score()));
                hits.add(row);
            }
            return hits;
        } catch (Exception e) {
            log.debug("KB vector search degraded: {}", e.getMessage());
            return List.of();
        }
    }

    private List<Map<String, Object>> fuseRrf(List<Map<String, Object>> lexical,
                                              List<Map<String, Object>> vector,
                                              int topN) {
        Map<String, Double> scores = new HashMap<>();
        Map<String, Map<String, Object>> byId = new LinkedHashMap<>();
        for (int i = 0; i < lexical.size(); i++) {
            Map<String, Object> row = lexical.get(i);
            String id = stringValue(row.get("chunkId"), "lex-" + i);
            scores.merge(id, 1.0 / (RRF_K + i + 1), Double::sum);
            byId.putIfAbsent(id, row);
        }
        for (int i = 0; i < vector.size(); i++) {
            Map<String, Object> row = vector.get(i);
            String id = stringValue(row.get("chunkId"), "vec-" + i);
            scores.merge(id, 1.0 / (RRF_K + i + 1), Double::sum);
            byId.putIfAbsent(id, row);
        }
        return scores.entrySet().stream()
                .sorted(Map.Entry.<String, Double>comparingByValue(Comparator.reverseOrder()))
                .limit(Math.max(topN, 1))
                .map(e -> {
                    Map<String, Object> row = new LinkedHashMap<>(byId.get(e.getKey()));
                    row.put("score", e.getValue());
                    row.put("channel", "rrf");
                    row.put("topScore", e.getValue());
                    return row;
                })
                .toList();
    }

    private List<Map<String, Object>> llmRerank(String query, List<Map<String, Object>> candidates) {
        if (deepSeekClient == null || candidates.isEmpty()) {
            return null;
        }
        List<Map<String, Object>> top = candidates.stream().limit(20).toList();
        String q = query == null ? "" : query;
        StringBuilder sb = new StringBuilder();
        sb.append("你是检索重排器。按与查询的相关性对候选片段重排，只输出 JSON：")
                .append("{\"rankedIds\":[\"chunkId\",...]}\n查询: ")
                .append(q, 0, Math.min(q.length(), 300))
                .append("\n候选:\n");
        for (int i = 0; i < top.size(); i++) {
            Map<String, Object> c = top.get(i);
            sb.append(i + 1).append(". id=").append(c.get("chunkId"))
                    .append(" | ").append(preview(stringValue(c.get("content"), ""), 160))
                    .append('\n');
        }
        try {
            String text = deepSeekClient.evaluateResume(sb.toString(), "KbReranker", "listwise_rerank");
            List<String> rankedIds = parseRankedIds(text);
            if (rankedIds.isEmpty()) return null;
            Map<String, Map<String, Object>> byId = new LinkedHashMap<>();
            for (Map<String, Object> c : top) {
                byId.put(stringValue(c.get("chunkId"), ""), c);
            }
            List<Map<String, Object>> ordered = new ArrayList<>();
            for (String id : rankedIds) {
                Map<String, Object> row = byId.remove(id);
                if (row != null) {
                    Map<String, Object> copy = new LinkedHashMap<>(row);
                    copy.put("rerankReason", "llm_listwise");
                    ordered.add(copy);
                }
            }
            ordered.addAll(byId.values());
            return ordered;
        } catch (Exception e) {
            log.debug("KB LLM rerank skipped: {}", e.getMessage());
            return null;
        }
    }

    private static List<String> parseRankedIds(String text) {
        if (!StringUtils.hasText(text)) return List.of();
        Matcher m = RANKED_IDS.matcher(text);
        if (!m.find()) return List.of();
        List<String> ids = new ArrayList<>();
        Matcher idm = Pattern.compile("\"([^\"]+)\"").matcher(m.group(1));
        while (idm.find()) {
            ids.add(idm.group(1));
        }
        return ids;
    }

    private Map<String, Map<String, Object>> loadAllChunksById() {
        Map<String, Map<String, Object>> byId = new HashMap<>();
        for (Map<String, Object> doc : loadManifest()) {
            String docId = stringValue(doc.get("docId"), "");
            Path chunksPath = kbRoot.resolve(docId + ".chunks.json");
            if (!Files.isRegularFile(chunksPath)) continue;
            try {
                List<Map<String, Object>> chunks = objectMapper.readValue(
                        chunksPath.toFile(), new TypeReference<>() {});
                for (Map<String, Object> chunk : chunks) {
                    byId.put(stringValue(chunk.get("chunkId"), ""), chunk);
                }
            } catch (Exception ignored) {
            }
        }
        return byId;
    }

    private boolean vectorReady() {
        return kbStore != null
                && embeddingModel != null
                && embeddingAvailability != null
                && embeddingAvailability.isOperational();
    }

    private List<Map<String, Object>> chunkDocument(String docId, String title, String content,
                                                    String docType, String tags) {
        List<String> blocks = splitIntoBlocks(content);
        List<Map<String, Object>> chunks = new ArrayList<>();
        for (int i = 0; i < blocks.size(); i++) {
            String block = blocks.get(i);
            Map<String, Object> chunk = new LinkedHashMap<>();
            chunk.put("chunkId", docId + "#chunk-" + i);
            chunk.put("docId", docId);
            chunk.put("title", title);
            chunk.put("docType", StringUtils.hasText(docType) ? docType : "general");
            chunk.put("sectionPath", inferSection(block));
            chunk.put("content", block);
            chunk.put("contentPreview", preview(block, 220));
            chunk.put("tokenEstimate", Math.max(1, block.length() / 2));
            Map<String, Object> metadata = new LinkedHashMap<>();
            metadata.put("docId", docId);
            metadata.put("chunkIndex", i);
            metadata.put("tags", splitTags(tags));
            metadata.put("source", "self_service_upload");
            metadata.put("embeddingStatus", "pending");
            metadata.put("indexStatus", "pending");
            metadata.put("embeddingProvider", embeddingProvider());
            metadata.put("indexVersion", indexVersion());
            metadata.put("fallbackStage", null);
            metadata.put("targetChunkChars", targetChunkChars);
            metadata.put("overlapChars", overlapChars);
            chunk.put("metadata", metadata);
            chunks.add(chunk);
        }
        return chunks;
    }

    private List<String> splitIntoBlocks(String content) {
        String normalized = content == null ? "" : content.replace('\u0000', ' ').trim();
        if (!StringUtils.hasText(normalized)) return List.of();

        List<String> segments = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        for (String line : normalized.split("\\R")) {
            String trimmed = line.trim();
            boolean boundary = trimmed.matches("^(#{1,6}\\s+.*|\\d+[.)、]\\s*.*|[一二三四五六七八九十]+[、.].*)$");
            if (boundary && current.length() > 0) {
                segments.add(current.toString().trim());
                current.setLength(0);
            }
            if (!StringUtils.hasText(trimmed)) {
                continue;
            }
            current.append(trimmed).append('\n');
        }
        if (current.length() > 0) segments.add(current.toString().trim());
        segments.removeIf(s -> !StringUtils.hasText(s));
        if (segments.isEmpty()) return List.of(normalized);

        List<String> chunks = new ArrayList<>();
        for (String seg : segments) {
            if (seg.length() <= targetChunkChars) {
                if (!chunks.isEmpty() && seg.length() < 30) {
                    int last = chunks.size() - 1;
                    chunks.set(last, chunks.get(last) + "\n" + seg);
                } else {
                    chunks.add(seg);
                }
                continue;
            }
            int start = 0;
            while (start < seg.length()) {
                int end = Math.min(seg.length(), start + targetChunkChars);
                chunks.add(seg.substring(start, end).trim());
                if (end >= seg.length()) break;
                start = Math.max(end - overlapChars, start + 1);
            }
        }
        return chunks.stream().filter(StringUtils::hasText).toList();
    }

    private List<Map<String, Object>> sampleChunks(List<Map<String, Object>> docs, int limit) {
        List<Map<String, Object>> result = new ArrayList<>();
        for (Map<String, Object> doc : docs) {
            if (result.size() >= limit) break;
            String docId = stringValue(doc.get("docId"), "");
            Path chunksPath = kbRoot.resolve(docId + ".chunks.json");
            if (!Files.isRegularFile(chunksPath)) continue;
            try {
                List<Map<String, Object>> chunks = objectMapper.readValue(
                        chunksPath.toFile(), new TypeReference<>() {});
                for (Map<String, Object> chunk : chunks) {
                    if (result.size() >= limit) break;
                    String indexStatus = normalizeIndexStatus(stringValue(doc.get("indexStatus"),
                            stringValue(doc.get("embeddingStatus"), "pending")));
                    result.add(enrichChunk(chunk, indexStatus, null));
                }
            } catch (Exception ignored) {
            }
        }
        return result;
    }

    private List<Map<String, Object>> loadManifest() {
        if (!Files.isRegularFile(manifestPath)) {
            return new ArrayList<>();
        }
        try {
            return objectMapper.readValue(manifestPath.toFile(), new TypeReference<>() {});
        } catch (Exception e) {
            return new ArrayList<>();
        }
    }

    private String inferSection(String content) {
        String first = content.lines().findFirst().orElse("content").trim();
        return first.length() > 60 ? first.substring(0, 60) : first;
    }

    private double scoreChunk(Map<String, Object> doc, Map<String, Object> chunk, List<String> terms) {
        String title = stringValue(chunk.get("title"), "");
        String section = stringValue(chunk.get("sectionPath"), "");
        String content = stringValue(chunk.get("content"), "");
        String metadata = stringValue(chunk.get("metadata"), "");
        if (!StringUtils.hasText(content)) return 0;
        if (terms.isEmpty()) return 0.1;
        double titleScore = scoreText(title, terms) * 0.25;
        double sectionScore = scoreText(section, terms) * 0.20;
        double contentScore = scoreText(content, expandTerms(terms)) * 0.42;
        double tagScore = scoreText(metadata, terms) * 0.10;
        double usageBoost = Math.min(intValue(doc.get("usageCount")) * 0.01, 0.03);
        return Math.min(1.0, titleScore + sectionScore + contentScore + tagScore + usageBoost);
    }

    private String rerankReason(Map<String, Object> doc, Map<String, Object> chunk, List<String> terms) {
        List<String> matched = new ArrayList<>();
        String joined = (stringValue(chunk.get("title"), "") + " "
                + stringValue(chunk.get("sectionPath"), "") + " "
                + stringValue(chunk.get("content"), "") + " "
                + stringValue(chunk.get("metadata"), "")).toLowerCase();
        for (String term : expandTerms(terms)) {
            if (joined.contains(term.toLowerCase()) && !matched.contains(term)) {
                matched.add(term);
            }
            if (matched.size() >= 6) break;
        }
        return matched.isEmpty()
                ? "fallback: no exact term matched, kept for low-score visibility"
                : "matched " + String.join(",", matched) + " in title/section/content/tags";
    }

    private double scoreText(String text, List<String> terms) {
        if (!StringUtils.hasText(text) || terms.isEmpty()) return 0;
        String lower = text.toLowerCase();
        long hit = terms.stream().filter(term -> lower.contains(term.toLowerCase())).count();
        return (double) hit / Math.max(terms.size(), 1);
    }

    private List<String> expandTerms(List<String> terms) {
        List<String> expanded = new ArrayList<>(terms);
        Map<String, List<String>> synonyms = Map.of(
                "agent", List.of("智能体", "harness", "workflow", "dag"),
                "rag", List.of("检索", "召回", "向量", "embedding", "rerank"),
                "java", List.of("spring", "后端", "jvm"),
                "项目", List.of("系统", "平台", "中台", "贡献", "真实性"),
                "风险", List.of("核验", "验证", "缺口", "边界"));
        for (String term : terms) {
            List<String> values = synonyms.get(term.toLowerCase());
            if (values != null) expanded.addAll(values);
        }
        return expanded.stream().distinct().toList();
    }

    private void markHits(List<Map<String, Object>> hits) {
        if (hits.isEmpty()) return;
        List<Map<String, Object>> docs = loadManifest();
        LocalDateTime now = LocalDateTime.now();
        boolean changed = false;
        for (Map<String, Object> hit : hits) {
            String docId = stringValue(hit.get("docId"), "");
            for (Map<String, Object> doc : docs) {
                if (docId.equals(stringValue(doc.get("docId"), ""))) {
                    doc.put("usageCount", intValue(doc.get("usageCount")) + 1);
                    doc.put("lastHitAt", now.toString());
                    changed = true;
                }
            }
        }
        if (changed) {
            try {
                objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), docs);
            } catch (IOException ignored) {
            }
        }
    }

    private Map<String, Object> enrichDocument(Map<String, Object> doc) {
        Map<String, Object> copy = new LinkedHashMap<>(doc);
        String indexStatus = normalizeIndexStatus(stringValue(doc.get("indexStatus"),
                stringValue(doc.get("embeddingStatus"), "pending")));
        copy.put("indexStatus", indexStatus);
        copy.putIfAbsent("embeddingProvider", embeddingProvider());
        copy.putIfAbsent("indexVersion", indexVersion());
        return copy;
    }

    private Map<String, Object> enrichChunk(Map<String, Object> chunk, String indexStatusHint,
                                            String fallbackStage) {
        Map<String, Object> copy = new LinkedHashMap<>(chunk);
        Map<String, Object> meta = new LinkedHashMap<>();
        Object existing = chunk.get("metadata");
        if (existing instanceof Map<?, ?> m) {
            m.forEach((k, v) -> meta.put(String.valueOf(k), v));
        }
        String docId = stringValue(chunk.get("docId"), stringValue(meta.get("docId"), ""));
        int chunkIndex = meta.containsKey("chunkIndex")
                ? intValue(meta.get("chunkIndex"))
                : parseChunkIndex(stringValue(chunk.get("chunkId"), ""));
        String indexStatus = normalizeIndexStatus(stringValue(
                indexStatusHint != null ? indexStatusHint : meta.get("indexStatus"),
                stringValue(meta.get("embeddingStatus"), "pending")));
        meta.put("docId", docId);
        meta.put("chunkIndex", chunkIndex);
        meta.putIfAbsent("source", "self_service_upload");
        meta.put("embeddingProvider", embeddingProvider());
        meta.put("indexVersion", indexVersion());
        meta.put("indexStatus", indexStatus);
        meta.put("embeddingStatus", stringValue(meta.get("embeddingStatus"), indexStatus));
        if (fallbackStage != null) {
            meta.put("fallbackStage", fallbackStage);
        }
        copy.put("docId", docId);
        copy.put("chunkIndex", chunkIndex);
        copy.put("source", stringValue(meta.get("source"), "self_service_upload"));
        copy.put("embeddingProvider", embeddingProvider());
        copy.put("indexVersion", indexVersion());
        copy.put("indexStatus", indexStatus);
        if (fallbackStage != null) {
            copy.put("fallbackStage", fallbackStage);
        }
        copy.put("metadata", meta);
        return copy;
    }

    private String normalizeIndexStatus(String raw) {
        if (!StringUtils.hasText(raw)) return "pending";
        return switch (raw.trim().toLowerCase()) {
            case "ready", "indexed", "success" -> "ready";
            case "indexing", "reindexing" -> "indexing";
            case "failed", "error" -> "failed";
            case "degraded", "lexical_only", "lexical" -> "degraded";
            case "pending" -> "pending";
            default -> "pending";
        };
    }

    private int parseChunkIndex(String chunkId) {
        int idx = chunkId.lastIndexOf("chunk-");
        if (idx < 0) return 0;
        try {
            return Integer.parseInt(chunkId.substring(idx + 6));
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private String embeddingProvider() {
        if (embeddingProperties == null || !StringUtils.hasText(embeddingProperties.getProvider())) {
            return "unknown";
        }
        return embeddingProperties.getProvider();
    }

    private String indexVersion() {
        if (embeddingProperties == null) {
            return "kb_v1_unknown";
        }
        return "kb_v1_" + embeddingProperties.resolveJdCollectionSuffix();
    }

    private String preview(String content, int max) {
        if (content == null) return "";
        String text = content.replaceAll("\\s+", " ").trim();
        return text.length() <= max ? text : text.substring(0, max);
    }

    private List<String> tokenize(String text) {
        if (!StringUtils.hasText(text)) return List.of();
        List<String> result = new ArrayList<>();
        for (String token : text.split("[\\s,，、/|；;:：()（）\\[\\]{}]+")) {
            String trimmed = token.trim();
            if (trimmed.length() >= 2 && trimmed.length() <= 32) {
                result.add(trimmed.toLowerCase());
            }
        }
        return result.stream().distinct().toList();
    }

    private List<String> splitTags(String tags) {
        if (!StringUtils.hasText(tags)) return List.of();
        return java.util.Arrays.stream(tags.split("[,，;；\\s]+")).filter(StringUtils::hasText).distinct().toList();
    }

    private String stringValue(Object value, String fallback) {
        return value == null ? fallback : String.valueOf(value);
    }

    private int intValue(Object value) {
        return value instanceof Number n ? n.intValue() : 0;
    }

    private double doubleValue(Object value) {
        return value instanceof Number n ? n.doubleValue() : 0D;
    }

    public record SearchResult(List<Map<String, Object>> chunks, String strategy,
                               int lexicalHits, int vectorHits, String fusion,
                               boolean rerankApplied, String fallbackStage,
                               List<String> fallbackChain) {}
}
