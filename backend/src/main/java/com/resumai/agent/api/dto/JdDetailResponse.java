package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

/**
 * JD 详情（含 description 全文）。
 */
public record JdDetailResponse(
        String jdId,
        String title,
        String category,
        String description,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
