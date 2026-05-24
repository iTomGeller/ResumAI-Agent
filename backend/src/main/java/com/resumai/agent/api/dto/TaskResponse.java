package com.resumai.agent.api.dto;

import java.time.LocalDateTime;
import java.util.List;

/**
 * 简历评估任务响应。
 *
 * <p>该响应面向前端工作台、性能大盘和 Agent 终端，聚合任务状态、耗时、
 * 推荐结论、评分和阶段性 Agent 输出。</p>
 */
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
        String matchedJdTitle,
        Double jdMatchScore
) {
}
