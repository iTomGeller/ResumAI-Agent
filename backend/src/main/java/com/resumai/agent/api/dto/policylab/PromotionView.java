package com.resumai.agent.api.dto.policylab;

import java.time.LocalDateTime;

public record PromotionView(
        Long id,
        String experimentId,
        String candidateId,
        String runType,
        String cohortKey,
        String previousPolicyId,
        String promotedPolicyId,
        String decision,
        String decidedBy,
        LocalDateTime decidedAt,
        String reason
) {
}
