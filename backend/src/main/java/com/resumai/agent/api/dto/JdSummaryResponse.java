package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

/**
 * JD 列表摘要（不含 description 全文）。
 */
public record JdSummaryResponse(
        String jdId,
        String title,
        String category,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
