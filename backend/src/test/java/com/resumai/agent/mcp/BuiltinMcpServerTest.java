package com.resumai.agent.mcp;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BuiltinMcpServerTest {

    private final BuiltinMcpServer server = new BuiltinMcpServer();

    @Test
    void noSyntheticEnrichmentToolsAreAdvertised() {
        Map<String, Object> response = server.handleJsonRpc(Map.of(
                "jsonrpc", "2.0",
                "id", 1,
                "method", "tools/list"
        ));

        Map<?, ?> result = (Map<?, ?>) response.get("result");
        assertEquals(List.of(), result.get("tools"));
    }

    @Test
    void legacySyntheticToolReturnsUnavailableEvidenceInsteadOfCandidateFacts() {
        Map<String, Object> response = server.handleJsonRpc(Map.of(
                "jsonrpc", "2.0",
                "id", 2,
                "method", "tools/call",
                "params", Map.of(
                        "name", "github_profile_search",
                        "arguments", Map.of("username", "same-name-user")
                )
        ));

        Map<?, ?> result = (Map<?, ?>) response.get("result");
        assertEquals(true, result.get("isError"));
        Map<?, ?> structured = (Map<?, ?>) result.get("structuredContent");
        assertEquals("unavailable", structured.get("status"));
        assertEquals("synthetic_tool_removed_configure_real_public_mcp", structured.get("reason"));
        assertFalse(structured.containsKey("repositories"));
        assertFalse(structured.containsKey("contributions"));

        Map<?, ?> evidence = (Map<?, ?>) structured.get("evidence");
        assertEquals("github", evidence.get("provider"));
        assertEquals("unverified", evidence.get("subjectBinding"));
        assertEquals(false, evidence.get("candidateFact"));
        assertEquals(false, evidence.get("syntheticFallback"));
        assertNull(evidence.get("sourceUrl"));
        assertNotNull(evidence.get("retrievedAt"));
    }

    @Test
    void bundledConfigHasRealDefaultsAndNoSyntheticFallback() throws Exception {
        try (InputStream input = getClass().getResourceAsStream("/mcp-servers.json")) {
            assertNotNull(input);
            JsonNode root = new ObjectMapper().readTree(input);
            assertFalse(root.path("evidencePolicy").path("allowSyntheticFallback").asBoolean(true));
            assertTrue(root.path("mcpServers").has("exa"));
            assertTrue(root.path("mcpServers").has("firecrawl"));
            assertEquals("https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa",
                    root.path("mcpServers").path("exa").path("url").asText());
            assertEquals("https://mcp.firecrawl.dev/v2/mcp",
                    root.path("mcpServers").path("firecrawl").path("url").asText());
            assertTrue(root.path("optionalMcpServers").has("github"));
            assertTrue(root.path("optionalMcpServers").has("tavily"));
            assertTrue(root.path("optionalMcpServers").has("brave-search"));
            assertTrue(root.path("optionalMcpServers").has("time"));
            assertTrue(root.path("optionalMcpServers").has("arxiv"));
        }
    }
}
