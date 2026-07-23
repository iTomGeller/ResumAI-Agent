package com.resumai.agent.api.dto;

/**
 * Page-context chip attached by the frontend Copilot. The server owns turn
 * disposition; these refs only enrich retrieval / reply grounding.
 */
public record ContextRefRequest(
        String type,
        String id,
        Integer revision,
        Integer version
) {
}
