package com.resumai.agent.api.dto;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class RagProvenanceTest {

    @Test
    void chunkProvenanceExposesOffsetsAndIds() {
        RagProvenance p = RagProvenance.chunk(
                "kb-1", "kb-1#chunk-0", "kb_v1_test",
                "2026-07-22T10:00:00", "2026-07-22T11:00:00",
                12, 88, "abcd1234");
        Map<String, Object> map = p.toMap();
        assertEquals("kb-1", map.get("documentId"));
        assertEquals("kb-1#chunk-0", map.get("chunkId"));
        assertEquals("kb_v1_test", map.get("version"));
        assertEquals(12, map.get("charStart"));
        assertEquals(88, map.get("charEnd"));
        assertEquals("abcd1234", map.get("contentHash"));
    }

    @Test
    void jdMatchResultKeepsChannelScoresAndProvenance() {
        JdMatchResult base = new JdMatchResult("jd-1", "Backend", "TECH", 0.82,
                List.of("skill"), List.of(), List.of(),
                0.7, 0.6, 0.5, 0.0);
        RagProvenance provenance = RagProvenance.document(
                "jd-1", "3", "2026-01-01T00:00:00", "2026-07-01T00:00:00");
        JdMatchResult withScores = base
                .withChannelScores(0.91, null)
                .withProvenance(provenance)
                .withRetrieval(0.01639, 0.91, 0.55);

        assertEquals(0.82, withScores.matchScore(), 1e-9);
        assertEquals(0.91, withScores.vectorScore(), 1e-9);
        assertEquals(0.55, withScores.bm25Score(), 1e-9);
        assertEquals(0.01639, withScores.rrfScore(), 1e-9);
        assertEquals(0.01639, withScores.retrievalScore(), 1e-9);
        assertNotNull(withScores.provenance());
        assertEquals("jd-1", withScores.provenance().documentId());
        assertEquals("3", withScores.provenance().version());
        assertNull(withScores.provenance().chunkId());
    }
}
