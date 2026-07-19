package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

import java.util.*;

public class EvidenceFusionTools {

    private final ObjectMapper objectMapper;

    public EvidenceFusionTools(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @Tool("对多个评估 Agent 提供的证据进行融合打分，计算加权可信度")
    public String evidence_merge(
            @P("JSON 格式的多源证据列表，包含各 Agent 的评估结果摘要") String evidenceJson) {
        try {
            Map<String, Object> merged = new LinkedHashMap<>();
            // This tool only inventories evidence. Confidence and consistency
            // require an actual judge result and must never be inferred from text length.
            merged.put("mergedConfidence", null);
            merged.put("totalEvidenceSources", countSources(evidenceJson));
            merged.put("consistencyScore", null);
            merged.put("conflictsDetected", evidenceJson.contains("矛盾") || evidenceJson.contains("不一致") ? 1 : 0);
            merged.put("fusionMethod", "structural_inventory_only");
            merged.put("syntheticFallback", false);
            return objectMapper.writeValueAsString(merged);
        } catch (Exception e) {
            return "{\"error\": \"Evidence merge failed: " + e.getMessage() + "\"}";
        }
    }

    private int countSources(String text) {
        int count = 0;
        if (text.contains("techEval") || text.contains("技术评估")) count++;
        if (text.contains("projectEval") || text.contains("项目评估")) count++;
        if (text.contains("risk") || text.contains("风险")) count++;
        if (text.contains("graph") || text.contains("图谱")) count++;
        if (text.contains("mcp") || text.contains("外部")) count++;
        return count;
    }
}
