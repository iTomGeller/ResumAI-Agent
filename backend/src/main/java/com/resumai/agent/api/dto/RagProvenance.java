package com.resumai.agent.api.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * RAG 文档 / chunk / 检索命中的溯源元数据。
 * <p>
 * {@code documentId} 与知识库 {@code docId}、JD {@code jdId} 对齐；
 * 字符偏移在切分时可得则填充，否则为 null。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record RagProvenance(
        String documentId,
        String chunkId,
        String version,
        String createdAt,
        String updatedAt,
        Integer charStart,
        Integer charEnd,
        String contentHash,
        String sourceUri,
        String originalFilename,
        String indexedAt,
        String parserVersion
) {
    public static RagProvenance document(String documentId, String version,
                                         String createdAt, String updatedAt) {
        return new RagProvenance(documentId, null, version, createdAt, updatedAt,
                null, null, null, null, null, null, null);
    }

    public static RagProvenance chunk(String documentId, String chunkId, String version,
                                      String createdAt, String updatedAt,
                                      Integer charStart, Integer charEnd, String contentHash) {
        return new RagProvenance(documentId, chunkId, version, createdAt, updatedAt,
                charStart, charEnd, contentHash, null, null, null, null);
    }

    public RagProvenance withOffsets(Integer start, Integer end) {
        return new RagProvenance(documentId, chunkId, version, createdAt, updatedAt,
                start, end, contentHash, sourceUri, originalFilename, indexedAt, parserVersion);
    }

    public RagProvenance withIndexedAt(String indexedAt) {
        return new RagProvenance(documentId, chunkId, version, createdAt, updatedAt,
                charStart, charEnd, contentHash, sourceUri, originalFilename, indexedAt, parserVersion);
    }

    public Map<String, Object> toMap() {
        Map<String, Object> map = new LinkedHashMap<>();
        if (documentId != null) map.put("documentId", documentId);
        if (chunkId != null) map.put("chunkId", chunkId);
        if (version != null) map.put("version", version);
        if (createdAt != null) map.put("createdAt", createdAt);
        if (updatedAt != null) map.put("updatedAt", updatedAt);
        if (charStart != null) map.put("charStart", charStart);
        if (charEnd != null) map.put("charEnd", charEnd);
        if (contentHash != null) map.put("contentHash", contentHash);
        if (sourceUri != null) map.put("sourceUri", sourceUri);
        if (originalFilename != null) map.put("originalFilename", originalFilename);
        if (indexedAt != null) map.put("indexedAt", indexedAt);
        if (parserVersion != null) map.put("parserVersion", parserVersion);
        return map;
    }
}
