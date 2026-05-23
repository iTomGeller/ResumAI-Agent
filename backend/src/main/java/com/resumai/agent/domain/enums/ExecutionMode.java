package com.resumai.agent.domain.enums;

/**
 * 简历评估任务执行模式。
 *
 * <p>SERIAL 用于稳定串行执行，DAG_CONCURRENT 用于多 Agent 并发压测与生产高吞吐场景。</p>
 */
public enum ExecutionMode {

    /**
     * 串行执行模式，所有子 Agent 按顺序阻塞执行。
     */
    SERIAL,

    /**
     * DAG 并发执行模式，子 Agent 按依赖关系并发执行。
     */
    DAG_CONCURRENT
}
