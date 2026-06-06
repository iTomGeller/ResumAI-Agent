package com.resumai.agent.ai;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.mcp.client.McpClient;
import dev.langchain4j.mcp.client.DefaultMcpClient;
import dev.langchain4j.mcp.client.transport.McpTransport;
import dev.langchain4j.mcp.client.transport.http.HttpMcpTransport;
import dev.langchain4j.service.tool.ToolProvider;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * MCP Tool Registry using langchain4j-mcp's McpClient and McpToolProvider.
 * Connects to standard MCP servers and exposes discovered tools as a ToolProvider
 * that can be injected into any langchain4j-agentic agent.
 */
@Component
public class McpToolRegistry {

    private static final Logger log = LoggerFactory.getLogger(McpToolRegistry.class);
    private static final ObjectMapper JSON = new ObjectMapper();

    @Value("${app.mcp.config-path:}")
    private String configPath;

    private final Map<String, McpServerEntry> serverConfigs = new ConcurrentHashMap<>();
    private final Map<String, McpClient> activeClients = new ConcurrentHashMap<>();

    @PostConstruct
    public void loadFromConfig() {
        if (configPath == null || configPath.isBlank()) {
            Path defaultPath = Path.of("mcp-servers.json");
            if (Files.exists(defaultPath)) {
                loadConfigFile(defaultPath);
            } else {
                log.info("No MCP servers config found, MCP tools disabled");
            }
            return;
        }
        Path path = Path.of(configPath);
        if (Files.exists(path)) {
            loadConfigFile(path);
        } else {
            log.info("MCP config path not found: {}", configPath);
        }
    }

    private void loadConfigFile(Path path) {
        try (InputStream is = Files.newInputStream(path)) {
            Map<String, Object> root = JSON.readValue(is, new TypeReference<>() {});
            Object servers = root.get("mcpServers");
            if (servers instanceof Map<?, ?> serversMap) {
                for (Map.Entry<?, ?> entry : serversMap.entrySet()) {
                    String serverId = entry.getKey().toString();
                    @SuppressWarnings("unchecked")
                    Map<String, Object> config = (Map<String, Object>) entry.getValue();
                    McpServerEntry mcpEntry = McpServerEntry.fromMap(config);
                    serverConfigs.put(serverId, mcpEntry);
                    connectSafe(serverId, mcpEntry);
                }
            }
        } catch (IOException e) {
            log.warn("Failed to load MCP config from {}: {}", path, e.getMessage());
        }
    }

    public void connect(String serverId, McpServerEntry entry) {
        serverConfigs.put(serverId, entry);
        connectSafe(serverId, entry);
    }

    private void connectSafe(String serverId, McpServerEntry entry) {
        try {
            McpTransport transport = createTransport(entry);
            if (transport == null) {
                log.warn("Unsupported transport for MCP server {}: {}", serverId, entry.transport());
                return;
            }
            McpClient client = new DefaultMcpClient.Builder()
                    .transport(transport)
                    .clientName("resumai-agent-" + serverId)
                    .build();
            activeClients.put(serverId, client);
            log.info("Connected MCP server: {} (transport={})", serverId, entry.transport());
        } catch (Exception e) {
            log.warn("Failed to connect MCP server {}: {}", serverId, e.getMessage());
        }
    }

    private McpTransport createTransport(McpServerEntry entry) {
        return switch (entry.transport()) {
            case "streamable-http", "http", "sse" -> {
                if (entry.url() == null || entry.url().isBlank()) yield null;
                yield new HttpMcpTransport.Builder()
                        .sseUrl(entry.url())
                        .timeout(Duration.ofSeconds(30))
                        .build();
            }
            default -> null;
        };
    }

    public void disconnect(String serverId) {
        McpClient client = activeClients.remove(serverId);
        if (client != null) {
            try {
                client.close();
            } catch (Exception e) {
                log.debug("Error closing MCP client {}: {}", serverId, e.getMessage());
            }
        }
        serverConfigs.remove(serverId);
        log.info("MCP server disconnected: {}", serverId);
    }

    /**
     * Returns a ToolProvider wrapping all active MCP clients.
     * Currently returns no-op; MCP tool execution to be wired in future iterations.
     */
    public ToolProvider asToolProvider() {
        return request -> null;
    }

    public boolean hasActiveConnections() {
        return !activeClients.isEmpty();
    }

    public List<String> getToolsForServer(String serverId) {
        McpClient client = activeClients.get(serverId);
        if (client == null) return List.of();
        try {
            return client.listTools().stream()
                    .map(ToolSpecification::name).toList();
        } catch (Exception e) {
            return List.of();
        }
    }

    public List<Map<String, Object>> listConnected() {
        List<Map<String, Object>> result = new ArrayList<>();
        serverConfigs.forEach((id, entry) -> {
            Map<String, Object> info = new LinkedHashMap<>();
            info.put("serverId", id);
            info.put("transport", entry.transport());
            info.put("url", entry.url());
            info.put("connected", activeClients.containsKey(id));
            result.add(info);
        });
        return result;
    }

    public void persistToConfig(String serverId, McpServerEntry entry) {
        Path path = Path.of(configPath != null && !configPath.isBlank() ? configPath : "mcp-servers.json");
        try {
            Map<String, Object> root;
            if (Files.exists(path)) {
                root = JSON.readValue(Files.readString(path), new TypeReference<>() {});
            } else {
                root = new LinkedHashMap<>();
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> servers = (Map<String, Object>) root.computeIfAbsent("mcpServers", k -> new LinkedHashMap<>());
            servers.put(serverId, entry.toMap());
            Files.writeString(path, JSON.writerWithDefaultPrettyPrinter().writeValueAsString(root));
        } catch (IOException e) {
            log.warn("Failed to persist MCP config: {}", e.getMessage());
        }
    }

    @PreDestroy
    public void shutdown() {
        activeClients.forEach((id, client) -> {
            try {
                client.close();
            } catch (Exception e) {
                log.debug("Error closing MCP client {}: {}", id, e.getMessage());
            }
        });
        activeClients.clear();
        serverConfigs.clear();
    }

    public record McpServerEntry(
            String transport,
            String url,
            List<String> command,
            Map<String, String> environment,
            String description
    ) {
        @SuppressWarnings("unchecked")
        public static McpServerEntry fromMap(Map<String, Object> map) {
            return new McpServerEntry(
                    (String) map.getOrDefault("transport", "streamable-http"),
                    (String) map.get("url"),
                    map.containsKey("command") ? (List<String>) map.get("command") : List.of(),
                    map.containsKey("environment") ? (Map<String, String>) map.get("environment") : Map.of(),
                    (String) map.getOrDefault("description", "")
            );
        }

        public Map<String, Object> toMap() {
            Map<String, Object> map = new LinkedHashMap<>();
            map.put("transport", transport);
            if (url != null) map.put("url", url);
            if (command != null && !command.isEmpty()) map.put("command", command);
            if (environment != null && !environment.isEmpty()) map.put("environment", environment);
            if (description != null && !description.isEmpty()) map.put("description", description);
            return map;
        }
    }
}
