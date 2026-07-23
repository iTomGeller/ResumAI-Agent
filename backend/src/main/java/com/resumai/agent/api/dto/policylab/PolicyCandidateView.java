package com.resumai.agent.api.dto.policylab;

import java.time.LocalDateTime;

public record PolicyCandidateView(
        String candidateId,
        String experimentId,
        String parentPolicyId,
        String bundlePolicyId,
        String configHash,
        String mutationReason,
        String status,
        String gateMetricsJson,
        LocalDateTime createTime
) {
}
