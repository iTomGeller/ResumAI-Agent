package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.service.ExternalProfileService;
import com.resumai.agent.service.ResumeRagService;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

public class TechEvalTools {

    private final ResumeRagService resumeRagService;
    private final ExternalProfileService externalProfileService;
    private final ObjectMapper objectMapper;

    public TechEvalTools(ResumeRagService resumeRagService,
                         ExternalProfileService externalProfileService,
                         ObjectMapper objectMapper) {
        this.resumeRagService = resumeRagService;
        this.externalProfileService = externalProfileService;
        this.objectMapper = objectMapper;
    }

    @Tool("从向量库检索候选人简历中与查询相关的证据片段，用于验证技术能力")
    public String milvus_resume_search(
            @P("搜索查询，如技能关键词、项目描述或岗位要求") String query,
            @P("返回结果数量，建议3-5") int topK) {
        try {
            var chunks = resumeRagService.retrieve(query, topK);
            return objectMapper.writeValueAsString(chunks);
        } catch (Exception e) {
            return "{\"error\": \"Resume search failed: " + e.getMessage() + "\"}";
        }
    }

    @Tool("仅根据简历中候选人声明的 GitHub/博客 URL 获取带来源边界的真实公开证据；失败返回 unavailable，禁止按姓名猜测或生成替代数据")
    public String github_enrichment(
            @P("候选人简历文本；只允许使用其中明确声明的 GitHub/博客 URL") String resumeText) {
        try {
            return externalProfileService.enrich(resumeText);
        } catch (Exception e) {
            try {
                return objectMapper.writeValueAsString(java.util.Map.of(
                        "status", "unavailable",
                        "reason", "external_evidence_failed",
                        "syntheticFallback", false
                ));
            } catch (Exception ignored) {
                return "{\"status\":\"unavailable\",\"reason\":\"external_evidence_failed\",\"syntheticFallback\":false}";
            }
        }
    }
}
