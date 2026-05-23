package com.resumai.agent.api.dto;

import java.util.List;

/**
 * GraphRAG 图谱响应。
 *
 * <p>MVP 阶段返回候选人与技能、项目、岗位、风险之间的模拟子图，
 * 后续阶段会替换为 Neo4j 子图查询结果。</p>
 */
public record GraphResponse(
        List<GraphNode> nodes,
        List<GraphEdge> edges
) {

    /**
     * 图谱节点。
     */
    public record GraphNode(String id, String label, String type, Integer score) {
    }

    /**
     * 图谱边。
     */
    public record GraphEdge(String from, String to, String label, Double confidence) {
    }
}
