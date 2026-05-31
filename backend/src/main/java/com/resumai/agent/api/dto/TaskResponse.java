package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;

public record TaskResponse(
        Long id,
        String traceId,
        String fileName,
        String jobCategory,
        String executionMode,
        String status,
        Integer overallScore,
        String recommendation,
        String summary,
        Long durationMs,
        Integer tokenCost,
        LocalDateTime createTime,
        LocalDateTime updateTime,
        List<String> strengths,
        List<String> risks,
        List<String> interviewQuestions,
        String resumeText,
        String resumeFileUrl,
        String resumeFileType,
        String matchedJdTitle,
        Double jdMatchScore,
        List<JdMatchResult> topJdMatches,
        String aiRecommendation,
        String decisionRationale,
        String riskSummary
) {
}
