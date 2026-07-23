package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;

/** 候选人详情，含申请列表。 */
public record CandidateDetailResponse(
        Long id,
        String displayName,
        String email,
        String phone,
        String identityKey,
        String identitySource,
        String resumeFingerprint,
        LocalDateTime createTime,
        LocalDateTime updateTime,
        List<CandidateApplicationResponse> applications
) {
}
