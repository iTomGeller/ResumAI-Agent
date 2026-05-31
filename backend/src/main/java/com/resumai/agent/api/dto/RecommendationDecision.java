package com.resumai.agent.api.dto;

public record RecommendationDecision(
        String recommendation,
        String aiRecommendation,
        String decisionRationale
) {
}
