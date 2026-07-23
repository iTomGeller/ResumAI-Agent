package com.resumai.agent.domain.enums;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

/**
 * Locks P0 storage-width contract for {@link PolicySelectionMode}.
 * Legacy label {@code CHAMPION_FALLBACK} must never be a storageValue
 * (it truncated VARCHAR(16)/VARCHAR(32) columns).
 */
class PolicySelectionModeTest {

    @Test
    void fallbackStorageFitsNarrowAndWideColumns() {
        String stored = PolicySelectionMode.FALLBACK.storageValue();
        assertTrue(stored.length() <= 16, "FALLBACK must fit VARCHAR(16): " + stored);
        assertTrue(stored.length() <= 32, "FALLBACK must fit VARCHAR(32): " + stored);
    }

    @Test
    void allStorageValuesFitVarchar32() {
        for (PolicySelectionMode mode : PolicySelectionMode.values()) {
            String stored = mode.storageValue();
            assertTrue(stored.length() <= 32,
                    mode + " storageValue too long for VARCHAR(32): " + stored);
        }
    }

    @Test
    void championFallbackIsNeverAStorageValue() {
        for (PolicySelectionMode mode : PolicySelectionMode.values()) {
            assertNotEquals("CHAMPION_FALLBACK", mode.storageValue());
        }
    }

    @Test
    void fromStorageMapsLegacyChampionFallbackToFallback() {
        assertEquals(PolicySelectionMode.FALLBACK,
                PolicySelectionMode.fromStorage("CHAMPION_FALLBACK"));
        assertEquals(PolicySelectionMode.FALLBACK,
                PolicySelectionMode.fromStorage("champion_fallback"));
    }
}
