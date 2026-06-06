package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * MCP Tools wrapper — calls the built-in MCP Server via standard JSON-RPC 2.0 protocol.
 * This demonstrates real MCP protocol usage: tools/call with proper JSON-RPC envelope.
 */
public class McpTools {

    private static final Logger log = LoggerFactory.getLogger(McpTools.class);

    private final String mcpServerUrl;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;
    private final AtomicInteger requestId = new AtomicInteger(1);

    public McpTools(String mcpServerUrl, ObjectMapper objectMapper) {
        this.mcpServerUrl = mcpServerUrl;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
    }

    @Tool("通过 MCP 协议搜索候选人 GitHub 开源贡献、仓库和活跃度（外部数据源）")
    public String mcp_github_profile_search(
            @P("技术关键词，用于搜索相关 GitHub 仓库和贡献") String keywords,
            @P("GitHub 用户名（可选，如简历中未提供则传空串）") String username) {
        return callMcpTool("github_profile_search", Map.of("keywords", keywords, "username", username));
    }

    @Tool("通过 MCP 协议搜索候选人技术博客和社区活跃度（外部数据源）")
    public String mcp_tech_blog_search(
            @P("搜索查询，候选人姓名或技术方向关键词") String query,
            @P("搜索平台：juejin/csdn/zhihu/all") String platforms) {
        return callMcpTool("tech_blog_search", Map.of("query", query, "platforms", platforms));
    }

    @Tool("通过 MCP 协议验证候选人 StackOverflow 技术问答活跃度（外部数据源）")
    public String mcp_stackoverflow_verify(
            @P("技术标签，如 java,spring-boot,redis") String tags) {
        return callMcpTool("stackoverflow_verify", Map.of("tags", tags));
    }

    private String callMcpTool(String toolName, Map<String, Object> arguments) {
        try {
            Map<String, Object> jsonRpcRequest = new LinkedHashMap<>();
            jsonRpcRequest.put("jsonrpc", "2.0");
            jsonRpcRequest.put("id", requestId.getAndIncrement());
            jsonRpcRequest.put("method", "tools/call");
            jsonRpcRequest.put("params", Map.of("name", toolName, "arguments", arguments));

            String body = objectMapper.writeValueAsString(jsonRpcRequest);
            log.debug("MCP call: {} -> {}", toolName, body);

            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(mcpServerUrl))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .timeout(Duration.ofSeconds(15))
                    .build();

            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            String responseBody = response.body();
            log.debug("MCP response: {}", responseBody);

            @SuppressWarnings("unchecked")
            Map<String, Object> jsonRpcResponse = objectMapper.readValue(responseBody, Map.class);
            @SuppressWarnings("unchecked")
            Map<String, Object> result = (Map<String, Object>) jsonRpcResponse.get("result");
            if (result != null && result.containsKey("content")) {
                @SuppressWarnings("unchecked")
                var contentList = (java.util.List<Map<String, Object>>) result.get("content");
                if (!contentList.isEmpty()) {
                    return (String) contentList.get(0).get("text");
                }
            }
            return objectMapper.writeValueAsString(result);
        } catch (Exception e) {
            log.warn("MCP tool call failed ({}): {}", toolName, e.getMessage());
            return "{\"error\": \"MCP call failed: " + e.getMessage() + "\", \"tool\": \"" + toolName + "\", \"protocol\": \"JSON-RPC 2.0\"}";
        }
    }
}
