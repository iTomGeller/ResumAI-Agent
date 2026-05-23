package com.resumai.agent.api.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

/**
 * HR 人工反馈请求。
 *
 * <p>该请求用于记录点赞、点踩、批注和修正动作，是后续 Meta-Agent
 * 反思调度规则与 Prompt 版本优化的数据来源。</p>
 */
public record FeedbackRequest(
        @NotBlank(message = "traceId 不能为空")
        String traceId,
        @Min(value = 1, message = "评分最低为 1")
        @Max(value = 5, message = "评分最高为 5")
        Integer ratingScore,
        String feedbackType,
        String humanComment,
        String fixAction,
        String reviewer
) {
}
