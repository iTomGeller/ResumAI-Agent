package com.resumai.agent.api.dto.policylab;

import java.time.LocalDateTime;
import java.util.Map;

public record PolicyExperimentEventView(
        Long id,
        String experimentId,
        Integer seq,
        String eventType,
        Map<String, Object> payload,
        LocalDateTime createTime
) {
}
