package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

public record JdDetailResponse(
        String jdId,
        String title,
        String category,
        String description,
        Integer version,
        String updatedBy,
        String tenantId,
        LocalDateTime createTime,
        LocalDateTime updateTime
) {
}
