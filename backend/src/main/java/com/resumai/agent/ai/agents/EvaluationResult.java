package com.resumai.agent.ai.agents;

import java.util.List;

/**
 * Structured result from the multi-agent evaluation orchestration.
 */
public record EvaluationResult(
        String finalReport,
        int overallScore,
        String recommendation,
        List<String> strengths,
        List<String> risks,
        List<String> interviewQuestions,
        String jdMatchResult,
        String techEvalResult,
        String riskResult,
        int totalAgentInvocations,
        long durationMs
) {}
