package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

/**
 * 候选人/任务列表项（不含完整 result_payload）。
 */
public record TaskListItemResponse(
        Long id,
        String traceId,
        String fileName,
        String jobCategory,
        String executionMode,
        String status,
        Integer overallScore,
        String recommendation,
        String summary,
        Long durationMs,
        Integer tokenCost,
        String matchedJdTitle,
        Double jdMatchScore,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
