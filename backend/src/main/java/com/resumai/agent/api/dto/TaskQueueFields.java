package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

public record TaskQueueFields(
        String queueStatus,
        String uploadedBy,
        String tenantId,
        Integer priority,
        LocalDateTime queuedAt,
        LocalDateTime startedAt,
        LocalDateTime finishedAt,
        Integer attemptCount,
        LocalDateTime nextRetryAt,
        String workerId
) {
}
