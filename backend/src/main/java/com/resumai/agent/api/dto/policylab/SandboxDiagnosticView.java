package com.resumai.agent.api.dto.policylab;

import java.time.LocalDateTime;

public record SandboxDiagnosticView(
        String sandboxId,
        String purpose,
        String experimentId,
        String trialId,
        String toolName,
        String status,
        Integer exitCode,
        Long durationMs,
        String error,
        String isolationMode,
        LocalDateTime createTime
) {
}
