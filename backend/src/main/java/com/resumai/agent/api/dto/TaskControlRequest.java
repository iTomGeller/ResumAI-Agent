package com.resumai.agent.api.dto;

import jakarta.validation.constraints.NotNull;

public record TaskControlRequest(@NotNull Action action) {
    public enum Action {
        PAUSE,
        RESUME,
        CANCEL
    }
}
