package com.resumai.agent.api.dto;

import jakarta.validation.constraints.NotBlank;

public record ConversationTurnRequest(
        @NotBlank(message = "clientMessageId 不能为空") String clientMessageId,
        @NotBlank(message = "content 不能为空") String content,
        Integer expectedRevision,
        /** collect（默认，排队补充）或 interrupt（打断当前运行）。 */
        String queueMode,
        /** Benchmark/回放专用：固定使用某个 PolicyBundle（记录为 FORCED 选择）。 */
        String forcedPolicyId
) {
    public String normalizedQueueMode() {
        return "interrupt".equalsIgnoreCase(queueMode) ? "interrupt" : "collect";
    }
}
