package com.resumai.agent.domain.enums;

/**
 * Persisted selection mode codes. Storage values must fit
 * {@code policy_selection.selection_mode VARCHAR(32)}.
 * Never persist UI labels longer than the column.
 */
public enum PolicySelectionMode {
    CHAMPION,
    FALLBACK,
    EXPLORE,
    EXPLOIT,
    THOMPSON,
    FORCED;

    public String storageValue() {
        return name();
    }

    public static PolicySelectionMode fromStorage(String raw) {
        if (raw == null || raw.isBlank()) {
            return FALLBACK;
        }
        // Legacy long label that truncated the VARCHAR(16) column.
        if ("CHAMPION_FALLBACK".equalsIgnoreCase(raw)) {
            return FALLBACK;
        }
        try {
            return PolicySelectionMode.valueOf(raw.trim().toUpperCase());
        } catch (IllegalArgumentException e) {
            return FALLBACK;
        }
    }
}
