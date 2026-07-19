package com.resumai.agent.domain.enums;

/**
 * 任务队列调度状态（与业务 status 解耦，用于排队/消费/重试）。
 */
public enum QueueStatus {
    QUEUED,
    RETRYING,
    RUNNING,
    PAUSED,
    RESUMING,
    SUPERSEDED,
    SUCCESS,
    FAILED,
    CANCELLED
}
