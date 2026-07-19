package com.resumai.agent.api.dto;

import jakarta.validation.constraints.NotBlank;

public record ConversationTurnRequest(
        @NotBlank(message = "clientMessageId 不能为空") String clientMessageId,
        @NotBlank(message = "content 不能为空") String content,
        Integer expectedRevision
) {
}
