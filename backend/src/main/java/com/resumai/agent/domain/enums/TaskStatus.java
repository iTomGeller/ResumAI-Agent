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

    /** 当前节点结束后写 checkpoint 并暂停。 */
    PAUSING,

    /** 已写入 checkpoint，等待用户继续。 */
    PAUSED,

    /** 正在从持久化 checkpoint 恢复。 */
    RESUMING,

    /** 已被同一会话中的新 revision 替代。 */
    SUPERSEDED,

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
