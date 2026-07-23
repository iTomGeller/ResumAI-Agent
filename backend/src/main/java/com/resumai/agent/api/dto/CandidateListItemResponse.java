package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;

/** 候选人列表行：一人一行，附最新申请摘要。 */
public record CandidateListItemResponse(
        Long id,
        String displayName,
        String email,
        String phone,
        String identitySource,
        int applicationCount,
        int assessmentCount,
        String latestStage,
        String latestOwnerHrId,
        Integer latestScore,
        String latestRecommendation,
        String latestJobCategory,
        String latestTraceId,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
