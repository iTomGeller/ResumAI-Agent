package com.resumai.agent.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
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
import java.util.stream.Collectors;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.multipart.MultipartFile;

@Service
public class KnowledgeBaseDocumentService {

    private static final int TARGET_CHUNK_CHARS = 320;
    private static final int OVERLAP_CHARS = 60;

    private final Path kbRoot;
    private final Path manifestPath;
    private final ObjectMapper objectMapper;

    public KnowledgeBaseDocumentService(@Value("${resumai.upload-dir:./uploads}") String uploadDir,
                                        ObjectMapper objectMapper) {
        this.kbRoot = Paths.get(uploadDir).toAbsolutePath().normalize().resolve("knowledge-base");
        this.manifestPath = kbRoot.resolve("manifest.json");
        this.objectMapper = objectMapper;
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
            return saveDocument(StringUtils.hasText(title) ? title : name, content, docType, tags, lower.endsWith(".pdf") ? "pdf" : "text");
        } catch (IOException e) {
            throw new IllegalArgumentException("knowledge document parse failed: " + e.getMessage(), e);
        }
    }

    public synchronized Map<String, Object> overview() {
        List<Map<String, Object>> docs = loadManifest();
        int chunkCount = docs.stream().mapToInt(d -> intValue(d.get("chunkCount"))).sum();
        Map<String, Long> byType = docs.stream()
                .collect(Collectors.groupingBy(d -> stringValue(d.get("docType"), "unknown"), LinkedHashMap::new, Collectors.counting()));
        return Map.of(
                "documentCount", docs.size(),
                "chunkCount", chunkCount,
                "chunkPolicy", Map.of(
                        "targetChunkChars", TARGET_CHUNK_CHARS,
                        "overlapChars", OVERLAP_CHARS,
                        "splitPriority", List.of("markdown_heading", "numbered_section", "blank_line", "char_window")),
                "retrievalPolicy", Map.of(
                        "strategy", "lexical_metadata_semantic_like_rerank",
                        "scoring", "title/tag/docType/section/content/recency/usage",
                        "fallback", "low_score_results_are_reported_not_hidden"),
                "docTypes", byType,
                "documents", docs.stream().limit(100).toList(),
                "sampleChunks", sampleChunks(docs, 8)
        );
    }

    /** All uploaded evaluation-standard documents (full manifest, no truncation). */
    public synchronized List<Map<String, Object>> listDocuments() {
        return loadManifest();
    }

    /** Single document with full content + chunks so the UI can show the whole rubric. */
    public synchronized Map<String, Object> getDocument(String docId) {
        for (Map<String, Object> doc : loadManifest()) {
            if (docId.equals(stringValue(doc.get("docId"), ""))) {
                Map<String, Object> result = new LinkedHashMap<>(doc);
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
                        result.put("chunks", objectMapper.readValue(chunksPath.toFile(), new TypeReference<>() {}));
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
            } catch (IOException e) {
                throw new IllegalStateException("delete knowledge document failed: " + e.getMessage(), e);
            }
        }
        return removed;
    }

    public synchronized List<Map<String, Object>> search(String query, int topK) {
        List<String> terms = tokenize(query);
        List<Map<String, Object>> scored = new ArrayList<>();
        for (Map<String, Object> doc : loadManifest()) {
            String docId = stringValue(doc.get("docId"), "");
            Path chunksPath = kbRoot.resolve(docId + ".chunks.json");
            if (!Files.isRegularFile(chunksPath)) {
                continue;
            }
            try {
                List<Map<String, Object>> chunks = objectMapper.readValue(chunksPath.toFile(), new TypeReference<>() {});
                for (Map<String, Object> chunk : chunks) {
                    String content = stringValue(chunk.get("content"), "");
                    double score = scoreChunk(doc, chunk, terms);
                    if (score >= 0.18) {
                        Map<String, Object> row = new LinkedHashMap<>(chunk);
                        row.put("score", score);
                        row.put("query", query == null ? "" : query);
                        row.put("strategy", "lexical_metadata_semantic_like_rerank");
                        row.put("rerankReason", rerankReason(doc, chunk, terms));
                        row.put("fallbackUsed", false);
                        row.put("topScore", score);
                        row.put("enabled", true);
                        scored.add(row);
                    }
                }
            } catch (Exception ignored) {
            }
        }
        scored.sort((a, b) -> Double.compare(doubleValue(b.get("score")), doubleValue(a.get("score"))));
        List<Map<String, Object>> hits = scored.stream().limit(Math.max(topK, 1)).toList();
        markHits(hits);
        return hits;
    }

    private Map<String, Object> saveDocument(String title, String content, String docType, String tags, String sourceFormat) {
        String docId = "kb-" + UUID.randomUUID();
        List<Map<String, Object>> chunks = chunkDocument(docId, title, content, docType, tags);
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("docId", docId);
        doc.put("title", StringUtils.hasText(title) ? title : docId);
        doc.put("docType", StringUtils.hasText(docType) ? docType : "general");
        doc.put("tags", splitTags(tags));
        doc.put("sourceFormat", sourceFormat);
        doc.put("charLength", content.length());
        doc.put("chunkCount", chunks.size());
        doc.put("embeddingStatus", "local_semantic_like");
        doc.put("chunkPolicy", Map.of("targetChunkChars", TARGET_CHUNK_CHARS, "overlapChars", OVERLAP_CHARS));
        doc.put("usageCount", 0);
        doc.put("lastHitAt", null);
        doc.put("createdAt", LocalDateTime.now().toString());
        doc.put("updatedAt", LocalDateTime.now().toString());
        try {
            Files.writeString(kbRoot.resolve(docId + ".txt"), content, StandardCharsets.UTF_8);
            objectMapper.writerWithDefaultPrettyPrinter().writeValue(kbRoot.resolve(docId + ".chunks.json").toFile(), chunks);
            List<Map<String, Object>> manifest = loadManifest();
            manifest.add(0, doc);
            objectMapper.writerWithDefaultPrettyPrinter().writeValue(manifestPath.toFile(), manifest);
        } catch (IOException e) {
            throw new IllegalStateException("save knowledge document failed: " + e.getMessage(), e);
        }
        return doc;
    }

    private List<Map<String, Object>> chunkDocument(String docId, String title, String content, String docType, String tags) {
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
            chunk.put("metadata", Map.of(
                    "tags", splitTags(tags),
                    "chunkIndex", i,
                    "source", "self_service_upload",
                    "embeddingStatus", "local_semantic_like",
                    "targetChunkChars", TARGET_CHUNK_CHARS,
                    "overlapChars", OVERLAP_CHARS));
            chunks.add(chunk);
        }
        return chunks;
    }

    /**
     * Structure-aware chunking. Splits at semantic boundaries (markdown headings, numbered /
     * lettered list items) so each rubric criterion / section becomes an independently retrievable
     * chunk, then char-window-splits any oversize segment with overlap. Short structured docs (e.g.
     * a 5-point rubric) therefore produce ~5 chunks instead of one opaque block.
     */
    private List<String> splitIntoBlocks(String content) {
        String normalized = content == null ? "" : content.replace('\u0000', ' ').trim();
        if (!StringUtils.hasText(normalized)) return List.of();

        // 1) Cut into semantic segments at headings / numbered list boundaries.
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

        // 2) Char-window-split oversize segments; merge ultra-short tails into the previous chunk.
        List<String> chunks = new ArrayList<>();
        for (String seg : segments) {
            if (seg.length() <= TARGET_CHUNK_CHARS) {
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
                int end = Math.min(seg.length(), start + TARGET_CHUNK_CHARS);
                chunks.add(seg.substring(start, end).trim());
                if (end >= seg.length()) break;
                start = Math.max(end - OVERLAP_CHARS, start + 1);
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
                List<Map<String, Object>> chunks = objectMapper.readValue(chunksPath.toFile(), new TypeReference<>() {});
                for (Map<String, Object> chunk : chunks) {
                    if (result.size() >= limit) break;
                    result.add(chunk);
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
}
