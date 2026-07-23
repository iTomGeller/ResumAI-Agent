package com.resumai.agent.api.dto.policylab;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

public record PolicyExperimentView(
        String experimentId,
        String kind,
        String status,
        Integer generation,
        String championPolicyId,
        String basePolicyId,
        String runType,
        String cohortKey,
        String evalDataset,
        String gateDataset,
        String safetyDataset,
        List<Long> seeds,
        Integer repeatsPerCase,
        Integer caseLimit,
        BigDecimal budgetCny,
        BigDecimal spentCny,
        BigDecimal progressPct,
        String progressPhase,
        boolean pauseRequested,
        boolean cancelRequested,
        boolean autoPromote,
        String note,
        String error,
        String createdBy,
        LocalDateTime startedAt,
        LocalDateTime finishedAt,
        LocalDateTime createTime
) {
}
