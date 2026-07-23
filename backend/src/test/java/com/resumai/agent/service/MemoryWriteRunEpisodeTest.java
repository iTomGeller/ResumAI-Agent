package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

class MemoryWriteRunEpisodeTest {

    @Test
    void hasBusinessArtifactsDetectsResumeFacts() throws Exception {
        MemoryService svc = new MemoryService(null, new ObjectMapper(), null);
        String json = new ObjectMapper().writeValueAsString(java.util.Map.of(
                "artifacts", java.util.Map.of("resumeFacts", java.util.Map.of("name", "A"))));
        assertTrue(svc.hasBusinessArtifacts(json));
        assertFalse(svc.hasBusinessArtifacts("{}"));
        assertFalse(svc.hasBusinessArtifacts(null));
        assertFalse(svc.hasBusinessArtifacts(
                new ObjectMapper().writeValueAsString(java.util.Map.of(
                        "artifacts", java.util.Map.of("scratch", "x")))));
    }
}
