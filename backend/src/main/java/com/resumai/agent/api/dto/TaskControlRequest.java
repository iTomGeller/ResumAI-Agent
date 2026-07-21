package com.resumai.agent.api.dto;

import jakarta.validation.constraints.NotNull;
import java.util.List;

/**
 * @param approvedPlan RESUME only: the user-confirmed (possibly edited) agent
 *                     pipeline from plan-approval mode; null keeps the
 *                     original plan untouched.
 */
public record TaskControlRequest(@NotNull Action action, List<String> approvedPlan) {
    public enum Action {
        PAUSE,
        RESUME,
        CANCEL
    }
}
