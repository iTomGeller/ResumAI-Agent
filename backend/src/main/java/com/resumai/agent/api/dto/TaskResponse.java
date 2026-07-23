package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

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
        String riskSummary,
        TaskQueueFields queue,
        String conversationId,
        Integer revisionNo,
        String workflowRunId,
        String baseWorkflowRunId,
        String supersedesTraceId,
        String supersededByTraceId,
        String evaluationBrief,
        List<String> invalidatedNodes,
        /** ReportAgent 的 Markdown 报告全文（评估报告 tab 的主体）。 */
        String fullReport,
        /** 校验过的结构化报告：dimensions/CandidateRisk/InterviewProbe/systemWarnings 等。 */
        Map<String, Object> structuredReport,
        Long candidateId,
        Long applicationId,
        /**
         * HR-facing evaluation lifecycle distinct from queue/runtime status:
         * NOT_STARTED | RUNNING | COMPLETED | SYSTEM_FAILED.
         */
        String evaluationState,
        TaskSystemError systemError
) {
    public record TaskSystemError(
            String code,
            String stage,
            boolean retryable,
            String message,
            String runId
    ) {
    }
}
