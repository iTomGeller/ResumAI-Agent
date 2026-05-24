package com.resumai.agent.api.dto;

public record JdMatchResult(
        String jdId,
        String title,
        String category,
        double score
) {
}
