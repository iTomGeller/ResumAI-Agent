package com.resumai.agent.api.dto;

import jakarta.validation.constraints.NotBlank;
import java.util.List;

/**
 * Public conversation turn. Queue mode and forced policy are intentionally
 * absent — the server decides disposition; benchmarks pin policy via internal APIs.
 */
public record ConversationTurnRequest(
        @NotBlank(message = "clientMessageId 不能为空") String clientMessageId,
        @NotBlank(message = "content 不能为空") String content,
        Integer expectedRevision,
        List<ContextRefRequest> contextRefs
) {
}
