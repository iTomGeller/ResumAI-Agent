package com.resumai.agent.service;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;

/**
 * Compatibility facade for the retired JSON memory store.
 *
 * <p>Business memory is owned exclusively by {@link MemoryService} and the
 * {@code memory_entry} table. The old files under {@code uploads/agent-memory}
 * are intentionally left untouched for recoverability, but this service no
 * longer creates, reads, searches, migrates, or updates them.</p>
 */
@Service
public class AgentMemoryService {

    /**
     * Kept for binary/source compatibility with older workflow callbacks.
     * Successful runs are persisted through MemoryService instead.
     */
    public void recordRunOutcome(String traceId, String status,
                                 String recommendation, Integer overallScore,
                                 Long durationMs, String summary) {
        // Legacy JSON memory writes are permanently disabled.
    }

    public Map<String, Object> overview() {
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("enabled", false);
        response.put("status", "disabled");
        response.put("storage", "memory_entry");
        response.put("memoryArchitecture", List.of("RECENT_CASE", "JOB_PROFILE"));
        response.put("message", "Legacy JSON memory is disabled; canonical business memory is shown above.");
        return response;
    }

    public Map<String, Object> search(String query, int topK) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("enabled", false);
        result.put("status", "disabled");
        result.put("query", query == null ? "" : query);
        result.put("hits", List.of());
        result.put("memoryArchitecture", List.of("RECENT_CASE", "JOB_PROFILE"));
        result.put("message", "Legacy JSON search is disabled; use the canonical memory search endpoint.");
        return result;
    }
}
