package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.service.ResumeRagService;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

public class ProjectEvalTools {

    private final ResumeRagService resumeRagService;
    private final ObjectMapper objectMapper;

    public ProjectEvalTools(ResumeRagService resumeRagService, ObjectMapper objectMapper) {
        this.resumeRagService = resumeRagService;
        this.objectMapper = objectMapper;
    }

    @Tool("从向量库检索与项目相关的证据片段，验证项目真实性和技术深度")
    public String milvus_resume_search(
            @P("项目相关搜索查询，如项目名称、核心技术、业务场景") String query,
            @P("返回结果数量，建议3-5") int topK) {
        try {
            var chunks = resumeRagService.retrieve(query, topK);
            return objectMapper.writeValueAsString(chunks);
        } catch (Exception e) {
            return "{\"error\": \"Project evidence search failed: " + e.getMessage() + "\"}";
        }
    }
}
