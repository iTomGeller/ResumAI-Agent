package com.resumai.agent.api.dto;

import java.util.List;

public record WorkflowResultRequest(
        String traceId,
        String workflowRunId,
        String status,
        String summary,
        Integer overallScore,
        String recommendation,
        List<String> strengths,
        List<String> risks,
        List<String> interviewQuestions,
        Long durationMs,
        Integer tokenCost,
        String failedNode,
        String errorMessage
) {}
