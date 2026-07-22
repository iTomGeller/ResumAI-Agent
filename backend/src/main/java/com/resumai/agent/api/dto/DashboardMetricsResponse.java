package com.resumai.agent.api.dto;

import java.util.Map;

/**
 * 性能与质量指标响应。
 *
 * <p>该响应支撑前端性能大盘，展示任务总量、成功率、平均耗时、Token 成本、
 * 执行模式对比和 Agent 耗时分布。</p>
 */
public record DashboardMetricsResponse(
        Integer totalTasks,
        Integer runningTasks,
        Integer successTasks,
        Integer failedTasks,
        Integer queuedTasks,
        Integer completedTasks,
        Integer recommendedTasks,
        Integer manualReviewTasks,
        Double averageDurationMs,
        Double averageScore,
        Integer totalTokenCost,
        Map<String, Long> modeDurationMs,
        Map<String, Long> agentDurationMs
) {
}
