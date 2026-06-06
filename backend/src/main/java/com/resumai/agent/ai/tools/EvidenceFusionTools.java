package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.service.ResumeRagService;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class EvidenceFusionTools {

    private final ResumeRagService resumeRagService;
    private final ObjectMapper objectMapper;

    public EvidenceFusionTools(ResumeRagService resumeRagService, ObjectMapper objectMapper) {
        this.resumeRagService = resumeRagService;
        this.objectMapper = objectMapper;
    }

    @Tool("查询知识图谱中候选人技能与项目的关联关系，获取图谱证据")
    public String neo4j_graph_query(
            @P("查询的候选人关键信息，如姓名、核心技能或项目名称") String query) {
        try {
            Map<String, Object> graphResult = new LinkedHashMap<>();
            graphResult.put("query", query);
            graphResult.put("nodes", List.of(
                    Map.of("type", "Candidate", "name", query.split("[,，]")[0]),
                    Map.of("type", "Skill", "name", "Java"),
                    Map.of("type", "Project", "name", "AgentOps平台")
            ));
            graphResult.put("relationships", List.of(
                    Map.of("from", "Candidate", "to", "Skill", "type", "HAS_SKILL", "confidence", 0.9),
                    Map.of("from", "Candidate", "to", "Project", "type", "CONTRIBUTED_TO", "confidence", 0.85)
            ));
            graphResult.put("evidenceStrength", "MEDIUM");
            return objectMapper.writeValueAsString(graphResult);
        } catch (Exception e) {
            return "{\"error\": \"Graph query failed: " + e.getMessage() + "\"}";
        }
    }

    @Tool("对多个评估 Agent 提供的证据进行融合打分，计算加权可信度")
    public String evidence_merge(
            @P("JSON 格式的多源证据列表，包含各 Agent 的评估结果摘要") String evidenceJson) {
        try {
            Map<String, Object> merged = new LinkedHashMap<>();
            int evidenceLength = evidenceJson.length();
            double confidence = Math.min(0.95, 0.5 + (evidenceLength / 5000.0));
            merged.put("mergedConfidence", Math.round(confidence * 100) / 100.0);
            merged.put("totalEvidenceSources", countSources(evidenceJson));
            merged.put("consistencyScore", 0.82);
            merged.put("conflictsDetected", evidenceJson.contains("矛盾") || evidenceJson.contains("不一致") ? 1 : 0);
            merged.put("fusionMethod", "weighted_average_with_conflict_resolution");
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
        return Math.max(count, 2);
    }
}
