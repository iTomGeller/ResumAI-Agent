package com.resumai.agent.api.dto;

import java.util.List;

/**
 * GraphRAG 图谱响应。
 */
public record GraphResponse(
        List<GraphNode> nodes,
        List<GraphEdge> edges,
        String source
) {

    public GraphResponse(List<GraphNode> nodes, List<GraphEdge> edges) {
        this(nodes, edges, "UNAVAILABLE");
    }

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
