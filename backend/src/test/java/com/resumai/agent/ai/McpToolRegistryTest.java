package com.resumai.agent.ai;

import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.mcp.McpToolProvider;
import dev.langchain4j.mcp.client.McpClient;
import dev.langchain4j.model.chat.request.json.JsonObjectSchema;
import dev.langchain4j.service.tool.ToolExecutionResult;
import dev.langchain4j.service.tool.ToolProviderRequest;
import dev.langchain4j.service.tool.ToolProviderResult;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class McpToolRegistryTest {

    @Test
    void providerDiscoversNamespacesAndDelegatesMcpExecution() {
        McpToolRegistry registry = new McpToolRegistry();
        McpToolProvider provider = assertInstanceOf(McpToolProvider.class, registry.asToolProvider());
        AtomicReference<ToolExecutionRequest> executedRequest = new AtomicReference<>();

        ToolSpecification remoteTool = ToolSpecification.builder()
                .name("web_search")
                .description("Searches a real provider")
                .parameters(JsonObjectSchema.builder().addStringProperty("query").required("query").build())
                .build();
        McpClient client = (McpClient) Proxy.newProxyInstance(
                getClass().getClassLoader(),
                new Class<?>[]{McpClient.class},
                (proxy, method, args) -> switch (method.getName()) {
                    case "key" -> "exa";
                    case "listTools" -> List.of(remoteTool);
                    case "executeTool" -> {
                        executedRequest.set((ToolExecutionRequest) args[0]);
                        yield ToolExecutionResult.builder()
                                .resultText("{\"status\":\"available\",\"sourceUrl\":\"https://example.test/source\"}")
                                .build();
                    }
                    case "close", "checkHealth", "setRoots", "subscribeToResource", "unsubscribeFromResource" -> null;
                    default -> method.getReturnType().equals(List.class) ? List.of() : null;
                });
        provider.addMcpClient(client);

        ToolProviderResult tools = provider.provideTools(new ToolProviderRequest(
                "test-memory", UserMessage.from("Find source-backed evidence")));
        ToolSpecification exposed = tools.toolSpecificationByName("exa__web_search");

        assertNotNull(exposed);
        assertTrue(exposed.description().contains("never synthesize a fallback"));
        assertEquals("exa", exposed.metadata().get("mcpServer"));
        assertEquals("source-backed-only", exposed.metadata().get("evidencePolicy"));
        assertEquals(false, exposed.metadata().get("syntheticFallback"));

        String executionResult = tools.toolExecutorByName("exa__web_search").execute(
                ToolExecutionRequest.builder()
                        .id("call-1")
                        .name("exa__web_search")
                        .arguments("{\"query\":\"LangGraph\"}")
                        .build(),
                "test-memory"
        );
        assertTrue(executionResult.contains("https://example.test/source"));
        assertNotNull(executedRequest.get());
        assertEquals("web_search", executedRequest.get().name());
    }

    @Test
    void configEntryPreservesRealRemoteHeadersAndCommandShape() {
        McpToolRegistry.McpServerEntry entry = McpToolRegistry.McpServerEntry.fromMap(Map.of(
                "transport", "streamable-http",
                "url", "https://api.githubcopilot.com/mcp/",
                "headers", Map.of(
                        "Authorization", "Bearer ${GITHUB_TOKEN}",
                        "X-MCP-Readonly", "true"
                ),
                "command", "npx",
                "args", List.of("-y", "server")
        ));

        assertEquals("https://api.githubcopilot.com/mcp/", entry.url());
        assertEquals("true", entry.headers().get("X-MCP-Readonly"));
        assertEquals(List.of("npx", "-y", "server"), entry.command());
        assertFalse(entry.toMap().isEmpty());
    }
}
