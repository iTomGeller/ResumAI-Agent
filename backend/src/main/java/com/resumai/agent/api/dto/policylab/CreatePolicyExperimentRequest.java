package com.resumai.agent.api.dto.policylab;

import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import java.math.BigDecimal;
import java.util.List;

public record CreatePolicyExperimentRequest(
        String kind,
        @NotBlank String basePolicyId,
        String runType,
        String cohortKey,
        @NotBlank String evalDataset,
        @NotBlank String gateDataset,
        @NotBlank String safetyDataset,
        @NotEmpty List<Long> seeds,
        @Min(1) int repeatsPerCase,
        @NotNull @Min(1) Integer caseLimit,
        @NotNull @DecimalMin("0.01") BigDecimal budgetCny,
        String note,
        /** Ignored — first version always forces false. */
        Boolean autoPromote
) {
}
