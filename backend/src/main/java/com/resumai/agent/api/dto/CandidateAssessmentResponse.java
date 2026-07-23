package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

/** 某候选人下的一次评估任务（assessment）。 */
public record CandidateAssessmentResponse(
        Long taskId,
        String traceId,
        Long applicationId,
        String fileName,
        String jobCategory,
        String status,
        Integer overallScore,
        String recommendation,
        String summary,
        Long durationMs,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
