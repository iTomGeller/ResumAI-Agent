package com.resumai.agent.api.dto.policylab;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record PolicyTrialView(
        String trialId,
        String experimentId,
        String candidateId,
        String datasetSplit,
        String caseId,
        Integer repeatNo,
        Long seed,
        String runId,
        String status,
        BigDecimal totalReward,
        BigDecimal costCny,
        Integer latencyMs,
        String runnerSandboxId,
        String evaluatorSandboxId,
        String error,
        LocalDateTime startedAt,
        LocalDateTime finishedAt
) {
}
