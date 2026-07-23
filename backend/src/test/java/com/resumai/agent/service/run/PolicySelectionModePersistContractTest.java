package com.resumai.agent.service.run;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.PolicyChampionAssignmentMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.enums.PolicySelectionMode;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * P0 persist contract: selection_mode values written by PolicyService paths
 * must fit {@code policy_selection.selection_mode} (historically VARCHAR(16),
 * now VARCHAR(32)). Never persist the legacy UI label {@code CHAMPION_FALLBACK}.
 */
class PolicySelectionModePersistContractTest {

    @Test
    void fallbackStorageFitsNarrowAndWideColumns() {
        String stored = PolicySelectionMode.FALLBACK.storageValue();
        assertTrue(stored.length() <= 16, "FALLBACK must fit VARCHAR(16): " + stored);
        assertTrue(stored.length() <= 32, "FALLBACK must fit VARCHAR(32): " + stored);
        assertEquals("FALLBACK", stored);
    }

    @Test
    void allEnumStorageValuesFitVarchar32() {
        for (PolicySelectionMode mode : PolicySelectionMode.values()) {
            String stored = mode.storageValue();
            assertTrue(stored.length() <= 32,
                    mode + " storageValue too long: " + stored + " (len=" + stored.length() + ")");
        }
    }

    @Test
    void championFallbackIsNotAnyStorageValue() {
        for (PolicySelectionMode mode : PolicySelectionMode.values()) {
            assertNotEquals("CHAMPION_FALLBACK", mode.storageValue(),
                    mode + " must not persist the legacy long label");
        }
    }

    @Test
    void fromStorageMapsLegacyChampionFallbackToFallback() {
        assertEquals(PolicySelectionMode.FALLBACK,
                PolicySelectionMode.fromStorage("CHAMPION_FALLBACK"));
    }

    @Test
    void chooseProductionChampionWithoutAssignmentPersistsShortFallbackMode() {
        PolicyChampionAssignmentMapper championMapper = mock(PolicyChampionAssignmentMapper.class);
        when(championMapper.selectOne(any())).thenReturn(null);

        PolicyService service = new PolicyService(
                null, null, null, championMapper, new ObjectMapper());

        PolicyBundleRow balanced = new PolicyBundleRow();
        balanced.setPolicyId("balanced");
        balanced.setConfig("{\"supportedRunTypes\":[\"full_evaluation\"]}");
        balanced.setStatus("ACTIVE");

        PolicyService.Selection selection = service.chooseProductionChampion(
                "full_evaluation", "default", List.of(balanced));

        assertEquals(PolicySelectionMode.FALLBACK, selection.mode());
        assertEquals("balanced", selection.bundle().getPolicyId());
        assertNull(selection.assignmentVersion());

        String stored = selection.mode().storageValue();
        assertTrue(stored.length() <= 16, "persist mode must fit VARCHAR(16): " + stored);
        assertTrue(stored.length() <= 32, "persist mode must fit VARCHAR(32): " + stored);
        assertNotEquals("CHAMPION_FALLBACK", stored);
        assertEquals("FALLBACK", stored);
    }
}
