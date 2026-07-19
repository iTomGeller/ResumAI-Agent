package com.resumai.agent.mcp;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Compatibility MCP endpoint for the application.
 *
 * <p>The previous implementation advertised GitHub, blog, and StackOverflow tools but returned
 * hard-coded candidate activity. Those synthetic tools are intentionally no longer advertised.
 * Legacy callers receive an explicit unavailable result with evidence metadata and no fallback
 * candidate facts. Real public providers are configured in {@code mcp-servers.json}.</p>
 */
@RestController
@RequestMapping("/mcp")
public class BuiltinMcpServer {

    private static final Logger log = LoggerFactory.getLogger(BuiltinMcpServer.class);
    private static final Set<String> REMOVED_SYNTHETIC_TOOLS = Set.of(
            "github_profile_search",
            "tech_blog_search",
            "stackoverflow_verify"
    );

    private final ObjectMapper objectMapper = new ObjectMapper();

    @PostMapping
    public Map<String, Object> handleJsonRpc(@RequestBody Map<String, Object> request) {
        Object id = request.get("id");
        Object methodValue = request.get("method");

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("jsonrpc", "2.0");
        response.put("id", id);

        if (!(methodValue instanceof String method) || method.isBlank()) {
            response.put("error", rpcError(-32600, "Invalid JSON-RPC request: method is required"));
            return response;
        }

        Map<String, Object> params;
        Object paramsValue = request.get("params");
        if (paramsValue == null) {
            params = Map.of();
        } else if (paramsValue instanceof Map<?, ?> paramsMap) {
            params = stringKeyMap(paramsMap);
        } else {
            response.put("error", rpcError(-32602, "Invalid params: expected an object"));
            return response;
        }

        log.debug("MCP request: method={}, id={}", method, id);
        switch (method) {
            case "initialize" -> response.put("result", handleInitialize());
            case "notifications/initialized" -> response.put("result", Map.of());
            case "tools/list" -> response.put("result", Map.of("tools", List.of()));
            case "tools/call" -> response.put("result", handleToolsCall(params));
            default -> response.put("error", rpcError(-32601, "Unknown method: " + method));
        }
        return response;
    }

    private Map<String, Object> handleInitialize() {
        return Map.of(
                "protocolVersion", "2025-06-18",
                "capabilities", Map.of("tools", Map.of("listChanged", false)),
                "serverInfo", Map.of(
                        "name", "resumai-evidence-policy-mcp",
                        "version", "2.0.0"
                ),
                "instructions", "No synthetic candidate enrichment is available here. "
                        + "Use configured public MCP providers and cite source URLs; failures mean unavailable."
        );
    }

    private Map<String, Object> handleToolsCall(Map<String, Object> params) {
        String toolName = params.get("name") instanceof String name ? name : "";
        String reason;
        if (toolName.isBlank()) {
            reason = "tool_name_required";
        } else if (REMOVED_SYNTHETIC_TOOLS.contains(toolName)) {
            reason = "synthetic_tool_removed_configure_real_public_mcp";
        } else {
            reason = "unknown_or_unconfigured_tool";
        }

        Map<String, Object> evidence = new LinkedHashMap<>();
        evidence.put("provider", providerFor(toolName));
        evidence.put("sourceUrl", null);
        evidence.put("retrievedAt", Instant.now().toString());
        evidence.put("subjectBinding", "unverified");
        evidence.put("candidateFact", false);
        evidence.put("syntheticFallback", false);

        Map<String, Object> unavailable = new LinkedHashMap<>();
        unavailable.put("status", "unavailable");
        unavailable.put("tool", toolName);
        unavailable.put("reason", reason);
        unavailable.put("evidence", evidence);
        unavailable.put("message", "No external source was queried and no candidate fact was produced. "
                + "Configure a real provider in mcp-servers.json.");

        log.info("MCP tool unavailable: tool={}, reason={}", toolName, reason);
        return Map.of(
                "content", List.of(Map.of("type", "text", "text", writeJson(unavailable))),
                "structuredContent", unavailable,
                "isError", true
        );
    }

    private String providerFor(String toolName) {
        return switch (toolName) {
            case "github_profile_search" -> "github";
            case "tech_blog_search" -> "public-web";
            case "stackoverflow_verify" -> "stackoverflow";
            default -> "unconfigured";
        };
    }

    private String writeJson(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JsonProcessingException e) {
            return "{\"status\":\"unavailable\",\"reason\":\"serialization_failed\"}";
        }
    }

    private Map<String, Object> rpcError(int code, String message) {
        return Map.of("code", code, "message", message);
    }

    private Map<String, Object> stringKeyMap(Map<?, ?> source) {
        Map<String, Object> result = new LinkedHashMap<>();
        source.forEach((key, value) -> result.put(String.valueOf(key), value));
        return result;
    }
}
