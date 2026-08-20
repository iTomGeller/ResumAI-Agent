package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.MemoryEntryMapper;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class MemoryWriteRunEpisodeTest {

    @Test
    void emptyScopedCandidateSetSkipsEmbeddingRecall() {
        MemoryEntryMapper mapper = mock(MemoryEntryMapper.class);
        MemoryVectorService vector = mock(MemoryVectorService.class);
        MemoryService svc = new MemoryService(mapper, new ObjectMapper(), vector);
        when(mapper.selectList(any())).thenReturn(List.of());

        List<Map<String, Object>> hits = svc.search(new MemoryService.SearchRequest(
                "Java", List.of("JOB_PROFILE"), "u1", "c1", "r1", 1,
                0.35, false));

        assertTrue(hits.isEmpty());
        verifyNoInteractions(vector);
    }

    @Test
    void identicalCurrentBuildWriteRefreshesProducerAndTtl() {
        MemoryEntryMapper mapper = mock(MemoryEntryMapper.class);
        MemoryVectorService vector = mock(MemoryVectorService.class);
        MemoryService svc = new MemoryService(mapper, new ObjectMapper(), vector);
        MemoryEntryRow existing = new MemoryEntryRow();
        existing.setMemoryId("mem-existing");
        existing.setConfidence(BigDecimal.valueOf(0.8));
        existing.setVersion(2);
        existing.setProducerVersion("old-build");
        existing.setExpiresAt(LocalDateTime.now().plusDays(2));
        when(mapper.selectOne(any())).thenReturn(existing);

        LocalDateTime before = LocalDateTime.now();
        MemoryEntryRow refreshed = svc.write(new MemoryService.WriteRequest(
                "SEMANTIC", "CONVERSATION", "u1", "c1", "r1",
                "候选人事实: Java", Map.of("_producerVersion", "build-2"),
                "candidate_fact", "candidate_profile:c1",
                0.9, "NORMAL", 90));

        assertEquals("build-2", refreshed.getProducerVersion());
        assertEquals(3, refreshed.getVersion());
        assertTrue(refreshed.getExpiresAt().isAfter(before.plusDays(89)));
        assertTrue(refreshed.getExpiresAt().isBefore(before.plusDays(91)));
    }

    @Test
    void hasBusinessArtifactsDetectsResumeFacts() throws Exception {
        MemoryService svc = new MemoryService(null, new ObjectMapper(), null);
        String json = new ObjectMapper().writeValueAsString(java.util.Map.of(
                "artifacts", java.util.Map.of("resumeFacts", java.util.Map.of("name", "A"))));
        assertTrue(svc.hasBusinessArtifacts(json));
        assertFalse(svc.hasBusinessArtifacts("{}"));
        assertFalse(svc.hasBusinessArtifacts(null));
        assertFalse(svc.hasBusinessArtifacts(
                new ObjectMapper().writeValueAsString(java.util.Map.of(
                        "artifacts", java.util.Map.of("scratch", "x")))));
    }

    @Test
    void cancelledRunNeverBecomesEpisodicConclusion() throws Exception {
        MemoryService svc = new MemoryService(null, new ObjectMapper(), null);
        String partial = new ObjectMapper().writeValueAsString(java.util.Map.of(
                "artifacts", java.util.Map.of(
                        "finalReport", java.util.Map.of(
                                "recommendation", "INTERVIEW_RECOMMEND"))));

        assertFalse(svc.shouldWriteRunEpisode("CANCELLED", partial));
        assertFalse(svc.shouldWriteRunEpisode("FAILED", partial));
        assertFalse(svc.shouldWriteRunEpisode("TIMED_OUT", partial));
        assertTrue(svc.shouldWriteRunEpisode("SUCCEEDED", partial));
    }

    @Test
    @SuppressWarnings("unchecked")
    void runtimeDurableWriteStagesThenPromotesOnlyAfterSuccess() throws Exception {
        MemoryEntryMapper mapper = mock(MemoryEntryMapper.class);
        MemoryVectorService vector = mock(MemoryVectorService.class);
        ObjectMapper json = new ObjectMapper();
        MemoryService svc = new MemoryService(mapper, json, vector);
        when(mapper.selectOne(any())).thenReturn(null);

        MemoryEntryRow staged = svc.stageRuntimeWrite(
                new MemoryService.WriteRequest(
                        "SEMANTIC", "CONVERSATION", "u1", "c1", "r1",
                        "候选人事实: Java", Map.of("factKey", "candidate_profile"),
                        "candidate_fact", "candidate_profile:c1",
                        0.9, "NORMAL", 180));

        assertEquals("WORKING", staged.getType());
        assertEquals("RUN", staged.getOwnerScope());
        Map<String, Object> stagedPayload = json.readValue(
                staged.getStructuredContent(), Map.class);
        Map<String, Object> pending = (Map<String, Object>)
                stagedPayload.get("_pendingPromotion");
        Map<String, Object> runtimeWrite = (Map<String, Object>)
                stagedPayload.get("_runtimeWrite");
        assertEquals("SEMANTIC", pending.get("type"));
        assertEquals("CONVERSATION", pending.get("ownerScope"));
        assertEquals("candidate_fact", pending.get("source"));
        assertEquals("PENDING_PROMOTION", runtimeWrite.get("kind"));
        assertNotNull(runtimeWrite.get("writeId"));

        staged.setStatus("ACTIVE");
        staged.setConfidence(BigDecimal.valueOf(0.9));
        when(mapper.selectList(any())).thenReturn(List.of(staged));
        List<MemoryEntryRow> promoted = svc.promoteRunMemories("r1");

        assertEquals(1, promoted.size());
        assertEquals("SEMANTIC", promoted.getFirst().getType());
        assertEquals("CONVERSATION", promoted.getFirst().getOwnerScope());
        Map<String, Object> durablePayload = json.readValue(
                promoted.getFirst().getStructuredContent(), Map.class);
        assertFalse(durablePayload.containsKey("_pendingPromotion"));
        assertFalse(durablePayload.containsKey("_runtimeWrite"));
        assertEquals("ARCHIVED", staged.getStatus());
    }

    @Test
    @SuppressWarnings("unchecked")
    void runtimeWorkingWriteGetsUniqueRowAndRollbackMarker() throws Exception {
        MemoryEntryMapper mapper = mock(MemoryEntryMapper.class);
        MemoryVectorService vector = mock(MemoryVectorService.class);
        ObjectMapper json = new ObjectMapper();
        MemoryService svc = new MemoryService(mapper, json, vector);
        when(mapper.selectOne(any())).thenReturn(null);

        MemoryEntryRow checkpoint = svc.write(new MemoryService.WriteRequest(
                "WORKING", "RUN", "u1", "c1", "run-1",
                "同一份运行上下文", Map.of("checkpoint", true),
                "run_input", "checkpoint-1", 1.0, "NORMAL", 2));
        MemoryEntryRow first = svc.stageRuntimeWrite(new MemoryService.WriteRequest(
                "WORKING", "RUN", "u1", "c1", "run-1",
                "同一份运行上下文", Map.of("scratch", true),
                "run_input", "working-1", 1.0, "NORMAL", 2));
        MemoryEntryRow second = svc.stageRuntimeWrite(new MemoryService.WriteRequest(
                "WORKING", "RUN", "u1", "c1", "run-1",
                "同一份运行上下文", Map.of("scratch", true),
                "run_input", "working-1", 1.0, "NORMAL", 2));

        // A runtime call cannot dedup onto a pre-existing checkpoint, and two
        // calls cannot share a row that a later cancellation might archive.
        assertNotEquals(checkpoint.getContentHash(), first.getContentHash());
        assertNotEquals(first.getContentHash(), second.getContentHash());
        assertNotEquals(first.getMemoryId(), second.getMemoryId());

        Map<String, Object> firstPayload = json.readValue(
                first.getStructuredContent(), Map.class);
        Map<String, Object> marker =
                (Map<String, Object>) firstPayload.get("_runtimeWrite");
        assertEquals("WORKING", marker.get("kind"));
        assertNotNull(marker.get("writeId"));
        assertFalse(firstPayload.containsKey("_pendingPromotion"));
    }

    @Test
    void durableStagingDedupKeyIncludesPromotionDestinationAndSource() {
        MemoryEntryMapper mapper = mock(MemoryEntryMapper.class);
        MemoryVectorService vector = mock(MemoryVectorService.class);
        MemoryService svc = new MemoryService(mapper, new ObjectMapper(), vector);
        when(mapper.selectOne(any())).thenReturn(null);

        MemoryEntryRow semanticConversation = svc.stageRuntimeWrite(
                durableRequest("SEMANTIC", "CONVERSATION", "candidate_fact"));
        MemoryEntryRow identicalRetry = svc.stageRuntimeWrite(
                durableRequest("SEMANTIC", "CONVERSATION", "candidate_fact"));
        MemoryEntryRow episodicConversation = svc.stageRuntimeWrite(
                durableRequest("EPISODIC", "CONVERSATION", "candidate_fact"));
        MemoryEntryRow semanticUser = svc.stageRuntimeWrite(
                durableRequest("SEMANTIC", "USER", "candidate_fact"));
        MemoryEntryRow semanticOtherSource = svc.stageRuntimeWrite(
                durableRequest("SEMANTIC", "CONVERSATION", "evaluation_insight"));
        MemoryEntryRow ordinaryWorking = svc.write(new MemoryService.WriteRequest(
                "WORKING", "RUN", "u1", "c1", "run-1",
                "相同内容", Map.of(), "candidate_fact", "fact-1",
                0.9, "NORMAL", 2));

        // Stable for an idempotent retry to the same destination.
        assertEquals(semanticConversation.getContentHash(),
                identicalRetry.getContentHash());
        // Never collapse targets that promote with different semantics.
        assertNotEquals(semanticConversation.getContentHash(),
                episodicConversation.getContentHash());
        assertNotEquals(semanticConversation.getContentHash(),
                semanticUser.getContentHash());
        assertNotEquals(semanticConversation.getContentHash(),
                semanticOtherSource.getContentHash());
        assertNotEquals(semanticConversation.getContentHash(),
                ordinaryWorking.getContentHash());
    }

    @Test
    void runtimeStrategyMustBeRealAttributableAndUserScoped() {
        MemoryEntryMapper mapper = mock(MemoryEntryMapper.class);
        MemoryVectorService vector = mock(MemoryVectorService.class);
        MemoryService svc = new MemoryService(mapper, new ObjectMapper(), vector);
        when(mapper.selectOne(any())).thenReturn(null);
        Map<String, Object> observed = Map.of(
                "factKey", "execution_strategy:full_evaluation:JD_TECH",
                "memoryKind", "execution_strategy",
                "strategyClass", "JD_TECH",
                "derivedFromRunId", "run-actual",
                "actualExecution", true,
                "candidateDataExcluded", true,
                "selectedAgents", List.of(
                        "TechAgent", "EvidenceAgent", "ReportAgent"));

        MemoryEntryRow accepted = svc.write(new MemoryService.WriteRequest(
                "PROCEDURAL", "USER", "u1", "c1", "run-actual",
                "简历评估执行策略[JD_TECH]: 已验证路由", observed,
                "runtime_strategy", "execution_strategy:JD_TECH",
                0.95, "NORMAL", 365));

        assertEquals("PROCEDURAL", accepted.getType());
        assertEquals("USER", accepted.getOwnerScope());
        assertThrows(IllegalArgumentException.class, () -> svc.write(
                new MemoryService.WriteRequest(
                        "PROCEDURAL", "GLOBAL", "u1", "c1", "run-actual",
                        "不能提升为全局事实", observed,
                        "runtime_strategy", "bad-global",
                        0.95, "NORMAL", 365)));
        assertThrows(IllegalArgumentException.class, () -> svc.write(
                new MemoryService.WriteRequest(
                        "PROCEDURAL", "USER", "u1", "c1", "different-run",
                        "伪造运行来源", observed,
                        "runtime_strategy", "bad-run",
                        0.95, "NORMAL", 365)));
    }

    @Test
    void postCheckRollbackTargetsOnlyThisMarkedRuntimeWrite() {
        UpdateWrapper<MemoryEntryRow> rollback =
                MemoryService.runtimeWriteArchive("run-1", "mem-staged-1");
        String sql = rollback.getSqlSegment();
        var values = rollback.getParamNameValuePairs().values();

        assertTrue(sql.contains("run_id"));
        assertTrue(sql.contains("memory_id"));
        assertTrue(sql.contains("owner_scope"));
        assertTrue(sql.contains("type"));
        assertTrue(sql.contains("status"));
        assertTrue(sql.contains("JSON_CONTAINS_PATH"));
        assertTrue(sql.contains("$._runtimeWrite.writeId"));
        assertTrue(values.contains("run-1"));
        assertTrue(values.contains("mem-staged-1"));
        assertTrue(values.contains("RUN"));
        assertTrue(values.contains("WORKING"));
        assertTrue(values.contains("ACTIVE"));
    }

    @Test
    void terminalRollbackCannotArchivePromotedOrCheckpointRows() {
        UpdateWrapper<MemoryEntryRow> rollback =
                MemoryService.pendingPromotionArchive("run-1", null);
        String sql = rollback.getSqlSegment();

        // Promoted durable rows fail RUN/WORKING predicates. A plain WORKING
        // checkpoint fails the pending-promotion JSON marker predicate.
        assertFalse(sql.contains("memory_id"));
        assertTrue(sql.contains("owner_scope"));
        assertTrue(sql.contains("type"));
        assertTrue(sql.contains("JSON_CONTAINS_PATH"));
        assertTrue(sql.contains("$._pendingPromotion"));
    }

    private static MemoryService.WriteRequest durableRequest(
            String type, String scope, String source) {
        return new MemoryService.WriteRequest(
                type, scope, "u1", "c1", "run-1",
                "相同内容", Map.of("factKey", "candidate_profile"),
                source, "fact-1", 0.9, "NORMAL", 180);
    }
}
