package com.resumai.agent.service.ops;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RagOpsResponse;
import com.resumai.agent.domain.entity.RunEvent;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class OpsRagObservabilityTest {

    private final ObjectMapper objectMapper = new ObjectMapper();
    private final OpsDebugService service = new OpsDebugService(
            null, null, null, null, null, null, objectMapper);

    @Test
    void joinsInvocationAndRetrievalTelemetryWithoutInventingQualityTruth() throws Exception {
        LocalDateTime base = LocalDateTime.of(2026, 7, 27, 10, 20, 30, 123_000_000);
        RunEvent started = event(1, "tool.started", base, Map.of(
                "toolCallId", "tc-rag-1",
                "arguments", Map.of("query", "Java Agent RAG", "topK", 5)));
        RunEvent completed = event(2, "tool.completed", base.plusNanos(20_000_000), Map.of(
                "toolCallId", "tc-rag-1",
                "durationMs", 20,
                "cacheHit", false,
                "resultPreview", Map.of(
                        "success", true,
                        "strategy", "hybrid_bm25_embedding",
                        "lexicalHits", 8,
                        "vectorHits", 7)));
        RunEvent telemetry = event(3, "retrieval.completed", base.plusNanos(22_000_000), Map.ofEntries(
                Map.entry("toolCallId", "tc-rag-1"),
                Map.entry("occurredAt", "2026-07-27T10:20:30.145Z"),
                Map.entry("requestedK", 5),
                Map.entry("returnedK", 2),
                Map.entry("uniqueDocuments", 2),
                Map.entry("candidateCount", 15),
                Map.entry("filteredCount", 4),
                Map.entry("deduplicatedCount", 3),
                Map.entry("strategy", "hybrid_bm25_embedding"),
                Map.entry("fusionStrategy", "rrf_k60"),
                Map.entry("stages", Map.of(
                        "queryRewriteMs", 2.5,
                        "embeddingMs", 4.0,
                        "retrievalMs", 10.0,
                        "fusionMs", 1.5,
                        "totalMs", 18.0)),
                Map.entry("chunks", List.of(
                        Map.of("chunkId", "c1", "docId", "d1",
                                "score", 0.82, "content", "first"),
                        Map.of("chunkId", "c2", "docId", "d2",
                                "score", 0.56, "content", "second")))));

        RagOpsResponse response = service.assembleRag(
                List.of(telemetry, completed, started), 100, null);

        assertEquals("rag-observability.v2", response.schemaVersion());
        assertEquals(1, response.count());
        var item = response.items().get(0);
        assertEquals("tc-rag-1", item.toolCallId());
        assertEquals("Java Agent RAG", item.query());
        assertEquals(5, item.requestedK());
        assertEquals(2, item.returnedK());
        assertEquals(0.82, item.topScore(), 0.0001);
        assertEquals(0.69, item.meanScore(), 0.0001);
        assertEquals(0.26, item.scoreSpread(), 0.0001);
        assertEquals(10.0, item.stages().retrievalMs(), 0.0001);
        assertEquals("2026-07-27T10:20:30.145Z", item.occurredAt());
        assertTrue(item.telemetryComplete());
        assertFalse(item.quality().groundTruthAvailable());
        assertNull(item.quality().precisionAtK());
        assertNull(item.quality().recallAtK());
        assertNull(item.quality().groundedness());
        assertEquals(1, response.summary().successCount());
        assertEquals(0, response.summary().zeroHitCount());
    }

    @Test
    void keepsLegacyMissingValuesNullAndSurfacesFailedZeroHitCall() throws Exception {
        LocalDateTime base = LocalDateTime.of(2026, 7, 27, 10, 30);
        RunEvent failed = event(7, "tool.failed", base, Map.of(
                "toolCallId", "tc-rag-failed",
                "arguments", Map.of("query", "不存在的知识", "topK", 3),
                "error", "backend unavailable"));
        RunEvent oldTelemetry = event(8, "retrieval.completed", base.plusSeconds(1), Map.of(
                "query", "legacy query",
                "hitCount", 0,
                "uniqueDocuments", 0,
                "latency", Map.of("embedding_search_ms", 12, "total_ms", 12)));

        RagOpsResponse response = service.assembleRag(
                List.of(failed, oldTelemetry), 100, null);

        assertEquals(2, response.count());
        var failedItem = response.items().stream()
                .filter(item -> "tc-rag-failed".equals(item.toolCallId()))
                .findFirst().orElseThrow();
        assertEquals("FAILED", failedItem.outcome());
        assertEquals("backend unavailable", failedItem.error());
        assertNull(failedItem.returnedK());
        assertNull(failedItem.zeroHit());

        var legacy = response.items().stream()
                .filter(item -> "legacy query".equals(item.query()))
                .findFirst().orElseThrow();
        assertEquals(0, legacy.returnedK());
        assertTrue(legacy.zeroHit());
        assertNull(legacy.topScore());
        assertEquals(12.0, legacy.stages().embeddingRetrievalMs(), 0.0001);
        assertFalse(legacy.telemetryComplete());
        assertTrue(response.warnings().stream().anyMatch(text -> text.contains("partial telemetry")));
    }

    @Test
    void requiresExplicitSuccessfulGroundTruthAndJudgeProvenance() throws Exception {
        LocalDateTime base = LocalDateTime.of(2026, 7, 27, 10, 40);
        RunEvent unverified = event(10, "retrieval.completed", base, Map.of(
                "toolCallId", "tc-rag-unverified-quality",
                "query", "unverified quality",
                "requestedK", 3,
                "returnedK", 2,
                "strategy", "hybrid",
                "durationMs", 10,
                "quality", Map.of(
                        "hasGroundTruth", true,
                        "precisionAtK", 0.99,
                        "recallAtK", 0.98,
                        "evaluator", "judge-without-status",
                        "groundedness", 0.97)));
        RunEvent verified = event(11, "retrieval.completed", base.plusSeconds(1), Map.of(
                "toolCallId", "tc-rag-verified-quality",
                "query", "verified quality",
                "requestedK", 3,
                "returnedK", 2,
                "strategy", "hybrid",
                "durationMs", 11,
                "quality", Map.ofEntries(
                        Map.entry("groundTruthDatasetId", "rag-gold-v3"),
                        Map.entry("groundTruthStatus", "SUCCEEDED"),
                        Map.entry("precisionAtK", 0.75),
                        Map.entry("recallAtK", 0.60),
                        Map.entry("judgeSource", "groundedness-judge-v2"),
                        Map.entry("judgeStatus", "COMPLETED"),
                        Map.entry("groundedness", 0.88))));
        RunEvent failedEvaluation = event(12, "retrieval.completed", base.plusSeconds(2), Map.of(
                "toolCallId", "tc-rag-failed-quality-evaluation",
                "query", "failed quality evaluation",
                "requestedK", 3,
                "returnedK", 2,
                "strategy", "hybrid",
                "durationMs", 12,
                "quality", Map.ofEntries(
                        Map.entry("labelSetId", "rag-gold-v3"),
                        Map.entry("groundTruthStatus", "FAILED"),
                        Map.entry("precisionAtK", 1.0),
                        Map.entry("recallAtK", 1.0),
                        Map.entry("judgeSource", "groundedness-judge-v2"),
                        Map.entry("judgeStatus", "FAILED"),
                        Map.entry("groundedness", 1.0))));

        RagOpsResponse response = service.assembleRag(
                List.of(failedEvaluation, verified, unverified), 100, null);

        var unverifiedQuality = response.items().stream()
                .filter(item -> "tc-rag-unverified-quality".equals(item.toolCallId()))
                .findFirst().orElseThrow().quality();
        assertFalse(unverifiedQuality.groundTruthAvailable());
        assertNull(unverifiedQuality.precisionAtK());
        assertNull(unverifiedQuality.recallAtK());
        assertNull(unverifiedQuality.groundedness());

        var failedEvaluationQuality = response.items().stream()
                .filter(item -> "tc-rag-failed-quality-evaluation".equals(item.toolCallId()))
                .findFirst().orElseThrow().quality();
        assertFalse(failedEvaluationQuality.groundTruthAvailable());
        assertNull(failedEvaluationQuality.precisionAtK());
        assertNull(failedEvaluationQuality.recallAtK());
        assertNull(failedEvaluationQuality.groundedness());

        var verifiedQuality = response.items().stream()
                .filter(item -> "tc-rag-verified-quality".equals(item.toolCallId()))
                .findFirst().orElseThrow().quality();
        assertTrue(verifiedQuality.groundTruthAvailable());
        assertEquals(0.75, verifiedQuality.precisionAtK(), 0.0001);
        assertEquals(0.60, verifiedQuality.recallAtK(), 0.0001);
        assertEquals(0.88, verifiedQuality.groundedness(), 0.0001);
        assertEquals("groundedness-judge-v2", verifiedQuality.judgeSource());
    }

    private RunEvent event(int seq, String type, LocalDateTime time,
                           Map<String, Object> payload) throws Exception {
        RunEvent event = new RunEvent();
        event.setRunId("run-rag-1");
        event.setTraceId("trace-rag-1");
        event.setConversationId("conversation-rag-1");
        event.setSeq(seq);
        event.setEventType(type);
        event.setAgentId("KnowledgeRetrievalAgent");
        event.setToolName("knowledge_search");
        event.setCreateTime(time);
        event.setPayload(objectMapper.writeValueAsString(payload));
        return event;
    }
}
