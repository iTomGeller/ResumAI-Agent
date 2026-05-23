package com.resumai.agent.domain.enums;

/**
 * Meta-Agent 自进化动作类型。
 */
public enum EvolutionType {

    /**
     * Prompt 模板升级。
     */
    PROMPT_UPDATE,

    /**
     * 系统调度规则升级。
     */
    RULE_UPDATE,

    /**
     * RAG 策略调整。
     */
    RAG_STRATEGY_UPDATE,

    /**
     * Agent 拓扑结构调整。
     */
    AGENT_TOPOLOGY_UPDATE,

    /**
     * 评分或重试阈值调整。
     */
    THRESHOLD_UPDATE
}
