package com.resumai.agent.api.dto;

import java.util.List;

public record JdMatchResult(
        String jdId,
        String title,
        String category,
        double score,
        List<String> matchReasons,
        List<String> gaps,
        List<String> interviewChecks,
        Double skillMatchScore,
        Double experienceMatchScore,
        Double projectMatchScore,
        Double riskPenalty
) {
    public JdMatchResult(String jdId, String title, String category, double score) {
        this(jdId, title, category, score, List.of(), List.of(), List.of(), null, null, null, null);
    }

    public JdMatchResult(String jdId, String title, String category, double score,
                         List<String> matchReasons, List<String> gaps, List<String> interviewChecks) {
        this(jdId, title, category, score, matchReasons, gaps, interviewChecks, null, null, null, null);
    }
}
