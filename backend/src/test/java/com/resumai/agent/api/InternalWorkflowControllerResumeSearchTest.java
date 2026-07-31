package com.resumai.agent.api;

import com.resumai.agent.ai.SkillProvider;
import com.resumai.agent.api.dto.InternalResumeSearchRequest;
import com.resumai.agent.service.AgentMemoryService;
import com.resumai.agent.service.ExternalProfileService;
import com.resumai.agent.service.HybridRagService;
import com.resumai.agent.service.InternalWorkflowService;
import com.resumai.agent.service.JdRagService;
import com.resumai.agent.service.ResumeRagService;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class InternalWorkflowControllerResumeSearchTest {

    @Test
    void resumeSearchReturnsScopedRankedItemsWithoutKnowledgeBaseSideCall() {
        InternalWorkflowService internal = mock(InternalWorkflowService.class);
        ResumeRagService resumeRag = mock(ResumeRagService.class);
        when(internal.authorize("token")).thenReturn(true);
        when(resumeRag.retrieveDetailed(
                anyString(), anyInt(), anyString(), any(), anyString()))
                .thenReturn(new ResumeRagService.RagRetrieveResult(
                        List.of("项目经历\nJava 支付平台"),
                        1, 0, null, false, "current_resume",
                        "section_bm25_rrf", null, "项目经历", false));
        InternalWorkflowController controller = new InternalWorkflowController(
                internal,
                resumeRag,
                mock(JdRagService.class),
                mock(HybridRagService.class),
                mock(ExternalProfileService.class),
                mock(SkillProvider.class),
                mock(AgentMemoryService.class));

        Map<String, Object> response = controller.resumeSearch(
                "token",
                new InternalResumeSearchRequest(
                        "项目经历", 5, "项目经历\nJava 支付平台", "", "hybrid"));

        assertEquals("current_resume", response.get("source"));
        assertEquals("current_resume", response.get("indexName"));
        assertEquals("rrf_structural_lexical", response.get("fusion"));
        assertTrue(response.containsKey("items"));
        assertFalse(response.containsKey("knowledgeHits"));
        List<?> items = (List<?>) response.get("items");
        assertEquals(1, items.size());
        Map<?, ?> first = (Map<?, ?>) items.get(0);
        assertEquals("current_resume", first.get("documentId"));
        assertTrue(first.containsKey("finalScore"));
    }
}
