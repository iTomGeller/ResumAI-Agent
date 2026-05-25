package com.resumai.agent.api.dto;

import java.util.List;

public record JdMatchResult(
        String jdId,
        String title,
        String category,
        double score,
        List<String> matchReasons,
        List<String> gaps,
        List<String> interviewChecks
) {
    public JdMatchResult(String jdId, String title, String category, double score) {
        this(jdId, title, category, score, List.of(), List.of(), List.of());
    }
}
