package com.resumai.agent.service;

import com.resumai.agent.config.EmbeddingAvailability;
import dev.langchain4j.model.embedding.EmbeddingModel;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;

class ResumeRagServiceTest {

    private final EmbeddingModel embeddingModel = mock(EmbeddingModel.class);
    private final ResumeRagService service = new ResumeRagService(
            null,
            embeddingModel,
            mock(EmbeddingAvailability.class),
            mock(MilvusVectorMaintenanceService.class));

    @Test
    void hybridSearchStaysInsideCurrentResumeAndUsesScopedChannels() {
        String resume = "教育背景\n某大学 计算机本科\n\n"
                + "工作经历\nJava Spring Boot 支付系统\n\n"
                + "项目经历\n高并发交易平台，使用 Kafka Redis，P99 降低 40%\n\n"
                + "技能特长\nJava MySQL Docker";

        ResumeRagService.RagRetrieveResult result = service.retrieveDetailed(
                "项目经历 技术方案 个人贡献", 5, resume, "", "hybrid");

        assertEquals("current_resume", result.backend());
        assertEquals("section_bm25_rrf", result.strategy());
        assertFalse(result.fallbackUsed());
        assertFalse(result.chunks().isEmpty());
        assertTrue(result.chunks().get(0).contains("项目经历"));
        assertTrue(result.chunks().stream().allMatch(resume::contains));
        verifyNoInteractions(embeddingModel);
    }

    @Test
    void unscopedEmbeddingFailsClosedToCurrentResumeText() {
        String resume = "项目经历\nJava RAG 平台";

        ResumeRagService.RagRetrieveResult result = service.retrieveDetailed(
                "RAG 项目", 5, resume, "", "embedding");

        assertTrue(result.fallbackUsed());
        assertEquals("candidate_scope_missing", result.errorType());
        assertTrue(result.chunks().stream().allMatch(resume::contains));
        verifyNoInteractions(embeddingModel);
    }

    @Test
    void englishProjectIntentRanksProjectSectionBeforeGenericMetrics() {
        String resume = "Summary\nSeven years backend experience\n\n"
                + "Projects\nPayment platform with Kafka and Redis\n\n"
                + "Highlights\nKey metrics improved steadily";

        ResumeRagService.RagRetrieveResult result = service.retrieveDetailed(
                "Projects architecture contribution metrics", 5,
                resume, "", "hybrid");

        assertTrue(result.chunks().get(0).contains("Projects"));
        verifyNoInteractions(embeddingModel);
    }
}
