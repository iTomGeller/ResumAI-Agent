package com.resumai.agent.service.run;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.enums.PolicySelectionMode;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Unit tests for run-type eligibility (no Spring / DB).
 * Uses package-visible helpers on PolicyService.
 */
class PolicyServiceEligibilityTest {

    private PolicyService service;

    @BeforeEach
    void setUp() {
        service = new PolicyService(null, null, null, null, new ObjectMapper());
    }

    @Test
    void lowCostDoesNotSupportFullEvaluation() {
        PolicyBundleRow low = bundle("low_cost", """
                {"supportedRunTypes":["quick_answer","followup","tech_match"],
                 "maxLlmCalls":6}
                """);
        assertFalse(service.supportsRunType(low, "full_evaluation"));
        assertTrue(service.supportsRunType(low, "quick_answer"));
    }

    @Test
    void legacyLowCostDenylistWithoutSupportedRunTypes() {
        PolicyBundleRow low = bundle("low_cost", "{\"maxLlmCalls\":6}");
        assertFalse(service.supportsRunType(low, "full_evaluation"));
        assertTrue(service.supportsRunType(low, "quick_answer"));
    }

    @Test
    void filterForTaskExcludesLowCostFromFullEvaluation() {
        PolicyBundleRow low = bundle("low_cost", """
                {"supportedRunTypes":["quick_answer","followup"]}
                """);
        PolicyBundleRow balanced = bundle("balanced", """
                {"supportedRunTypes":["full_evaluation","quick_answer"]}
                """);
        List<PolicyBundleRow> filtered = service.filterForTask(
                List.of(low, balanced),
                "full_evaluation",
                Map.of("runType", "full_evaluation"));
        assertEquals(1, filtered.size());
        assertEquals("balanced", filtered.get(0).getPolicyId());
    }

    @Test
    void resolveRunTypePrefersContext() {
        assertEquals("tech_match",
                PolicyService.resolveRunType("full_evaluation",
                        Map.of("runType", "tech_match")));
    }

    @Test
    void productionDecisionPicksChampionOnly() {
        PolicyBundleRow low = bundle("low_cost", """
                {"supportedRunTypes":["full_evaluation"]}
                """);
        low.setIsChampion(0);
        PolicyBundleRow balanced = bundle("balanced", """
                {"supportedRunTypes":["full_evaluation"]}
                """);
        balanced.setIsChampion(1);
        service.setEpsilon(1.0); // would always explore if bandit ran
        PolicyService.Selection selection = service.chooseChampion(List.of(low, balanced));
        assertEquals("balanced", selection.bundle().getPolicyId());
        assertEquals(PolicySelectionMode.CHAMPION, selection.mode());
        assertEquals("CHAMPION", selection.mode().storageValue());
        assertEquals(0.0, selection.epsilonUsed());
        assertNull(selection.assignmentVersion());
    }

    @Test
    void productionFallbackWhenNoChampionFlag() {
        PolicyBundleRow a = bundle("strict_evidence", "{\"supportedRunTypes\":[\"full_evaluation\"]}");
        a.setIsChampion(0);
        PolicyBundleRow balanced = bundle("balanced", "{\"supportedRunTypes\":[\"full_evaluation\"]}");
        balanced.setIsChampion(0);
        PolicyService.Selection selection = service.chooseChampion(List.of(a, balanced));
        assertEquals("balanced", selection.bundle().getPolicyId());
        assertEquals(PolicySelectionMode.FALLBACK, selection.mode());
        assertEquals("FALLBACK", selection.mode().storageValue());
    }

    @Test
    void productionChampionFallsBackWithoutAssignment() {
        PolicyBundleRow low = bundle("low_cost", """
                {"supportedRunTypes":["quick_answer"]}
                """);
        PolicyBundleRow balanced = bundle("balanced", """
                {"supportedRunTypes":["full_evaluation","quick_answer"]}
                """);
        PolicyService.Selection selection = service.chooseProductionChampion(
                "full_evaluation", "default", List.of(balanced, low));
        assertEquals("balanced", selection.bundle().getPolicyId());
        assertEquals(PolicySelectionMode.FALLBACK, selection.mode());
        assertNull(selection.assignmentVersion());
    }

    @Test
    void cohortKeyDefaultsToDefault() {
        assertEquals("default", PolicyService.cohortKey(null));
        assertEquals("default", PolicyService.cohortKey(Map.of()));
        assertEquals("cohort-a", PolicyService.cohortKey(Map.of("cohortKey", "cohort-a")));
    }

    private static PolicyBundleRow bundle(String id, String config) {
        PolicyBundleRow row = new PolicyBundleRow();
        row.setPolicyId(id);
        row.setConfig(config);
        row.setStatus("ACTIVE");
        return row;
    }
}
