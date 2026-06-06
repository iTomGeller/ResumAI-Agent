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

    @Tool("根据简历内容抓取候选人GitHub、博客等公开技术资料，补充外部证据")
    public String github_enrichment(
            @P("候选人简历文本，从中提取GitHub/博客链接") String resumeText) {
        try {
            String result = externalProfileService.enrich(resumeText);
            return result.isEmpty() ? "{\"profiles\": [], \"note\": \"未发现公开技术资料链接\"}" : result;
        } catch (Exception e) {
            return "{\"error\": \"GitHub enrichment failed: " + e.getMessage() + "\"}";
        }
    }
}
