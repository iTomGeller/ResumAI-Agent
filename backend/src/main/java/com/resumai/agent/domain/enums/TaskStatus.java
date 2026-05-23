package com.resumai.agent.domain.enums;

/**
 * 简历评估任务生命周期状态。
 */
public enum TaskStatus {

    /**
     * 任务已创建但尚未进入队列。
     */
    CREATED,

    /**
     * 任务已进入 Redis Stream 等待消费。
     */
    QUEUED,

    /**
     * 任务正在执行中。
     */
    RUNNING,

    /**
     * 任务全部成功完成。
     */
    SUCCESS,

    /**
     * 任务部分 Agent 成功，部分 Agent 失败或降级。
     */
    PARTIAL_SUCCESS,

    /**
     * 任务执行失败。
     */
    FAILED,

    /**
     * 任务被用户或系统取消。
     */
    CANCELLED
}
