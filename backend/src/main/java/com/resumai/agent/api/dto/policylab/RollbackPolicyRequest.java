package com.resumai.agent.api.dto.policylab;

import jakarta.validation.constraints.NotBlank;

public record RollbackPolicyRequest(
        @NotBlank String toPolicyId,
        String reason
) {
}
