package com.resumai.agent.domain.enums;

/**
 * RAG 检索策略类型。
 */
public enum RagStrategyType {

    /**
     * 基于 Milvus 的纯向量召回策略。
     */
    DENSE_VECTOR,

    /**
     * 基于 Neo4j 的图谱检索策略。
     */
    GRAPH,

    /**
     * 向量召回与图谱召回融合后的混合策略。
     */
    HYBRID
}
