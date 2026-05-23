package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

/**
 * HR 人工反馈响应。
 */
public record FeedbackResponse(
        Long id,
        String traceId,
        Integer ratingScore,
        String feedbackType,
        String humanComment,
        String fixAction,
        String reviewer,
        LocalDateTime createTime
) {
}
