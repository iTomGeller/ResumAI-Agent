package com.resumai.agent.api.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.util.List;
import java.util.Map;

/**
 * 知识库混合检索响应：命中列表携带 provenance 与分项召回分。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record KnowledgeSearchResponse(
        List<Map<String, Object>> chunks,
        String strategy,
        int lexicalHits,
        int vectorHits,
        String fusion,
        boolean rerankApplied,
        String fallbackStage,
        List<String> fallbackChain,
        String queryId,
        String retrievedAt,
        Long latencyMs,
        Long retrievalMs,
        Long fusionMs,
        Long rerankMs,
        Integer candidateCount,
        String rerankProvider,
        Double rerankBeforeTopScore,
        Double rerankAfterTopScore,
        String rerankBeforeTopChunkId,
        String rerankAfterTopChunkId,
        Integer rerankMovedCount
) {
}
