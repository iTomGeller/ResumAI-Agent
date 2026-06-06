package com.resumai.agent.api.dto;

import jakarta.validation.constraints.NotBlank;

public record UpsertJdRequest(
        String jdId,
        @NotBlank String title,
        @NotBlank String category,
        @NotBlank String description,
        Integer version
) {
}
