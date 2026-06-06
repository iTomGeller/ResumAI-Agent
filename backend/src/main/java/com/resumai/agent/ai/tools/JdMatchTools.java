package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.service.JdRagService;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

public class JdMatchTools {

    private final JdRagService jdRagService;
    private final ObjectMapper objectMapper;

    public JdMatchTools(JdRagService jdRagService, ObjectMapper objectMapper) {
        this.jdRagService = jdRagService;
        this.objectMapper = objectMapper;
    }

    @Tool("从岗位库检索与候选人最匹配的岗位列表，返回匹配的岗位名称、匹配原因和差距分析")
    public String milvus_jd_search(
            @P("候选人简历摘要或关键技能关键词") String query,
            @P("返回匹配岗位数量，建议3-5") int topK) {
        try {
            var results = jdRagService.matchTopJds(query, topK);
            return objectMapper.writeValueAsString(results);
        } catch (Exception e) {
            return "{\"error\": \"JD search failed: " + e.getMessage() + "\"}";
        }
    }

    @Tool("从岗位描述中提取结构化的招聘要求，包括必备技能、加分项、经验年限等")
    public String jd_requirements_extract(
            @P("岗位描述全文或岗位名称") String jdText) {
        try {
            return jdRagService.extractRequirements(jdText);
        } catch (Exception e) {
            return "{\"error\": \"JD requirements extraction failed: " + e.getMessage() + "\"}";
        }
    }
}
