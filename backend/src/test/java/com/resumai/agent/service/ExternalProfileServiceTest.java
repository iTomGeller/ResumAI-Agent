package com.resumai.agent.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ExternalProfileServiceTest {

    private static final ObjectMapper JSON = new ObjectMapper();
    private final ExternalProfileService service = new ExternalProfileService();

    @Test
    void noDeclaredUrlIsExplicitlyUnavailableWithoutSyntheticFallback() throws Exception {
        JsonNode result = JSON.readTree(service.enrich("Java engineer with Spring experience"));

        assertEquals("unavailable", result.path("status").asText());
        assertEquals("no_candidate_declared_external_url", result.path("reason").asText());
        assertFalse(result.path("syntheticFallback").asBoolean(true));
        assertTrue(result.path("github").isEmpty());
        assertTrue(result.path("declaredLinks").isEmpty());
    }

    @Test
    void declaredBlogUrlIsBoundToItsSourceButNotPromotedToCandidateFact() throws Exception {
        JsonNode result = JSON.readTree(service.enrich(
                "Writing samples: https://dev.to/candidate/source-backed-post"));

        assertEquals("declared-only", result.path("status").asText());
        JsonNode evidence = result.path("declaredLinks").get(0);
        assertEquals("https://dev.to/candidate/source-backed-post", evidence.path("sourceUrl").asText());
        assertEquals("candidate-declared-url-unverified-ownership", evidence.path("subjectBinding").asText());
        assertFalse(evidence.path("candidateFact").asBoolean(true));
        assertFalse(evidence.path("syntheticFallback").asBoolean(true));
    }
}
