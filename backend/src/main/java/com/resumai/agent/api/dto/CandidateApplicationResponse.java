package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

public record CandidateApplicationResponse(
        Long id,
        Long candidateId,
        String jobCategory,
        String jobId,
        String stage,
        String ownerHrId,
        Long latestTaskId,
        String latestTraceId,
        Integer latestScore,
        String latestRecommendation,
        String sourceFileName,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
