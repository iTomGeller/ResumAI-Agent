package com.resumai.agent.ai;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.mcp.McpToolProvider;
import dev.langchain4j.mcp.client.DefaultMcpClient;
import dev.langchain4j.mcp.client.McpClient;
import dev.langchain4j.mcp.client.transport.McpTransport;
import dev.langchain4j.mcp.client.transport.http.StreamableHttpMcpTransport;
import dev.langchain4j.mcp.client.transport.stdio.StdioMcpTransport;
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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Discovers and executes real MCP tools through LangChain4j's {@link McpToolProvider}.
 *
 * <p>Each provider gets a namespaced tool name, and every external tool carries the source-backed
 * evidence boundary in its description and metadata. A server is marked available only after a
 * successful MCP initialize/tools-list exchange; failed providers are isolated from healthy ones.</p>
 */
@Component
public class McpToolRegistry {

    private static final Logger log = LoggerFactory.getLogger(McpToolRegistry.class);
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Duration CONNECT_TIMEOUT = Duration.ofSeconds(15);
    private static final Duration TOOL_TIMEOUT = Duration.ofSeconds(30);
    private static final Pattern ENV_PLACEHOLDER = Pattern.compile("\\$\\{([A-Za-z_][A-Za-z0-9_]*)}");
    private static final String EVIDENCE_GUARDRAIL =
            "External evidence only: candidate facts require a candidate-declared URL or handle "
                    + "and a source URL. Empty, rate-limited, or failed results mean unavailable; "
                    + "never synthesize a fallback.";

    @Value("${app.mcp.config-path:}")
    private String configPath;

    private final Map<String, McpServerEntry> serverConfigs = new ConcurrentHashMap<>();
    private final Map<String, McpClient> activeClients = new ConcurrentHashMap<>();
    private final Map<String, List<String>> discoveredTools = new ConcurrentHashMap<>();
    private final Map<String, String> serverStates = new ConcurrentHashMap<>();
    private final McpToolProvider toolProvider = McpToolProvider.builder()
            .mcpClients(List.of())
            .failIfOneServerFails(false)
            .toolSpecificationMapper((client, tool) -> sourceBackedSpecification(client.key(), tool))
            .build();

    @PostConstruct
    public void loadFromConfig() {
        String configuredPath = configPath == null ? "" : configPath.trim();
        if (!configuredPath.isEmpty()) {
            Path path = Path.of(configuredPath);
            if (Files.exists(path)) {
                loadConfigFile(path);
                return;
            }
            if (!"mcp-servers.json".equals(path.getFileName().toString())) {
                log.warn("MCP config path not found: {}", configuredPath);
                return;
            }
        }

        try (InputStream input = McpToolRegistry.class.getResourceAsStream("/mcp-servers.json")) {
            if (input == null) {
                log.info("No MCP server config found; external MCP tools are unavailable");
                return;
            }
            loadConfig(input, "classpath:/mcp-servers.json");
        } catch (IOException e) {
            log.warn("Failed to close bundled MCP config: {}", e.getMessage());
        }
    }

    private void loadConfigFile(Path path) {
        try (InputStream input = Files.newInputStream(path)) {
            loadConfig(input, path.toString());
        } catch (IOException e) {
            log.warn("Failed to load MCP config from {}: {}", path, e.getMessage());
        }
    }

    private void loadConfig(InputStream input, String source) {
        try {
            Map<String, Object> root = JSON.readValue(input, new TypeReference<>() {});
            Object servers = root.get("mcpServers");
            if (!(servers instanceof Map<?, ?> serversMap)) {
                log.info("MCP config {} has no active mcpServers", source);
                return;
            }
            for (Map.Entry<?, ?> rawEntry : serversMap.entrySet()) {
                String serverId = String.valueOf(rawEntry.getKey());
                if (!(rawEntry.getValue() instanceof Map<?, ?> rawConfig)) {
                    serverStates.put(serverId, "unavailable: invalid server config");
                    continue;
                }
                Map<String, Object> config = stringKeyMap(rawConfig);
                McpServerEntry entry = McpServerEntry.fromMap(config);
                serverConfigs.put(serverId, entry);
                Activation activation = activation(config);
                if (!activation.requested()) {
                    serverStates.put(serverId, activation.reason());
                    log.info("MCP server {} not activated: {}", serverId, activation.reason());
                    continue;
                }
                connectSafe(serverId, entry);
            }
        } catch (IOException e) {
            log.warn("Failed to parse MCP config {}: {}", source, e.getMessage());
        }
    }

    /** Connect a dynamically supplied server, failing explicitly when discovery cannot complete. */
    public void connect(String serverId, McpServerEntry entry) {
        Objects.requireNonNull(serverId, "serverId");
        Objects.requireNonNull(entry, "entry");
        serverConfigs.put(serverId, entry);
        if (!connectSafe(serverId, entry)) {
            throw new IllegalStateException(serverStates.getOrDefault(serverId, "MCP connection failed"));
        }
    }

    private synchronized boolean connectSafe(String serverId, McpServerEntry entry) {
        McpClient candidate = null;
        try {
            McpTransport transport = createTransport(entry);
            candidate = new DefaultMcpClient.Builder()
                    .transport(transport)
                    .key(serverId)
                    .clientName("resumai-agent-" + serverId)
                    .clientVersion("1.0.0")
                    .initializationTimeout(CONNECT_TIMEOUT)
                    .toolExecutionTimeout(TOOL_TIMEOUT)
                    .cacheToolList(true)
                    .build();

            List<String> toolNames = candidate.listTools().stream()
                    .map(ToolSpecification::name)
                    .toList();
            if (toolNames.isEmpty()) {
                throw new IllegalStateException("no tools discovered");
            }

            McpClient previous = activeClients.put(serverId, candidate);
            if (previous != null) {
                toolProvider.removeMcpClient(previous);
                closeQuietly(serverId, previous);
            }
            toolProvider.addMcpClient(candidate);
            discoveredTools.put(serverId, toolNames);
            serverStates.put(serverId, "available");
            log.info("MCP server {} available via {} with tools {}", serverId, entry.transport(), toolNames);
            return true;
        } catch (Exception e) {
            if (candidate != null) {
                closeQuietly(serverId, candidate);
            }
            String reason = safeReason(e);
            serverStates.put(serverId, "unavailable: " + reason);
            log.warn("MCP server {} unavailable: {}", serverId, reason);
            return false;
        }
    }

    private McpTransport createTransport(McpServerEntry entry) {
        String transportName = entry.transport() == null
                ? ""
                : entry.transport().trim().toLowerCase(Locale.ROOT).replace('_', '-');
        return switch (transportName) {
            case "streamable-http", "http" -> {
                if (entry.url() == null || entry.url().isBlank()) {
                    throw new IllegalArgumentException("remote MCP server requires a URL");
                }
                Map<String, String> headers = resolveValues(entry.headers());
                StreamableHttpMcpTransport.Builder builder = new StreamableHttpMcpTransport.Builder()
                        .url(entry.url())
                        .timeout(CONNECT_TIMEOUT)
                        .followRedirects(true);
                if (!headers.isEmpty()) {
                    builder.customHeaders(headers);
                }
                yield builder.build();
            }
            case "stdio" -> {
                if (entry.command() == null || entry.command().isEmpty()) {
                    throw new IllegalArgumentException("stdio MCP server requires a command");
                }
                StdioMcpTransport.Builder builder = new StdioMcpTransport.Builder()
                        .command(entry.command());
                Map<String, String> environment = resolveValues(entry.environment());
                if (!environment.isEmpty()) {
                    builder.environment(environment);
                }
                yield builder.build();
            }
            default -> throw new IllegalArgumentException("unsupported MCP transport: " + transportName);
        };
    }

    public synchronized void disconnect(String serverId) {
        McpClient client = activeClients.remove(serverId);
        if (client != null) {
            toolProvider.removeMcpClient(client);
            closeQuietly(serverId, client);
        }
        serverConfigs.remove(serverId);
        discoveredTools.remove(serverId);
        serverStates.remove(serverId);
        log.info("MCP server disconnected: {}", serverId);
    }

    /** Returns the live provider used by AiServices for discovery and tool execution. */
    public ToolProvider asToolProvider() {
        return toolProvider;
    }

    public boolean hasActiveConnections() {
        return !activeClients.isEmpty();
    }

    public List<String> getToolsForServer(String serverId) {
        return List.copyOf(discoveredTools.getOrDefault(serverId, List.of()));
    }

    public List<Map<String, Object>> listConnected() {
        List<Map<String, Object>> result = new ArrayList<>();
        serverConfigs.forEach((id, entry) -> {
            Map<String, Object> info = new LinkedHashMap<>();
            String state = serverStates.getOrDefault(id, "not connected");
            info.put("serverId", id);
            info.put("transport", entry.transport());
            info.put("url", entry.url());
            info.put("status", activeClients.containsKey(id)
                    ? "available"
                    : (state.startsWith("disabled") ? "disabled" : "unavailable"));
            info.put("reason", state);
            info.put("tools", getToolsForServer(id));
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
            Map<String, Object> servers = (Map<String, Object>) root.computeIfAbsent(
                    "mcpServers", ignored -> new LinkedHashMap<>());
            servers.put(serverId, entry.toMap());
            Files.writeString(path, JSON.writerWithDefaultPrettyPrinter().writeValueAsString(root));
        } catch (IOException e) {
            log.warn("Failed to persist MCP config: {}", e.getMessage());
        }
    }

    @PreDestroy
    public synchronized void shutdown() {
        activeClients.forEach((id, client) -> {
            toolProvider.removeMcpClient(client);
            closeQuietly(id, client);
        });
        activeClients.clear();
        discoveredTools.clear();
        serverConfigs.clear();
        serverStates.clear();
    }

    private static ToolSpecification sourceBackedSpecification(String serverId, ToolSpecification tool) {
        String originalDescription = tool.description() == null ? "" : tool.description().trim();
        String description = originalDescription.contains(EVIDENCE_GUARDRAIL)
                ? originalDescription
                : (originalDescription + "\n\n" + EVIDENCE_GUARDRAIL).trim();
        return tool.toBuilder()
                .name(toolName(serverId, tool.name()))
                .description(description)
                .addMetadata("mcpServer", serverId)
                .addMetadata("evidencePolicy", "source-backed-only")
                .addMetadata("syntheticFallback", false)
                .build();
    }

    private static String toolName(String serverId, String toolName) {
        String safeServer = String.valueOf(serverId).replaceAll("[^A-Za-z0-9_-]", "_");
        String safeTool = String.valueOf(toolName).replaceAll("[^A-Za-z0-9_-]", "_");
        return safeServer + "__" + safeTool;
    }

    private static Map<String, String> resolveValues(Map<String, String> rawValues) {
        if (rawValues == null || rawValues.isEmpty()) {
            return Map.of();
        }
        Map<String, String> resolved = new LinkedHashMap<>();
        rawValues.forEach((name, rawValue) -> resolved.put(name, resolveEnvironment(rawValue)));
        return resolved;
    }

    private static String resolveEnvironment(String rawValue) {
        String value = rawValue == null ? "" : rawValue;
        Matcher matcher = ENV_PLACEHOLDER.matcher(value);
        StringBuilder resolved = new StringBuilder();
        while (matcher.find()) {
            String envName = matcher.group(1);
            String envValue = System.getenv(envName);
            if (envValue == null || envValue.isBlank()) {
                throw new IllegalStateException("missing environment variable " + envName);
            }
            matcher.appendReplacement(resolved, Matcher.quoteReplacement(envValue));
        }
        matcher.appendTail(resolved);
        return resolved.toString();
    }

    private static Activation activation(Map<String, Object> config) {
        Object enabled = config.getOrDefault("enabled", true);
        if (enabled instanceof Boolean bool && !bool) {
            return new Activation(false, "disabled by config");
        }
        if (!(enabled instanceof Boolean) && !"auto".equalsIgnoreCase(String.valueOf(enabled))) {
            return new Activation(false, "invalid enabled value");
        }
        Object required = config.get("requiredEnv");
        if (required instanceof List<?> names) {
            List<String> missing = names.stream()
                    .map(String::valueOf)
                    .filter(name -> {
                        String value = System.getenv(name);
                        return value == null || value.isBlank();
                    })
                    .toList();
            if (!missing.isEmpty()) {
                return new Activation(false, "unavailable: missing environment " + missing);
            }
        }
        return new Activation(true, "enabled");
    }

    private static Map<String, Object> stringKeyMap(Map<?, ?> source) {
        Map<String, Object> result = new LinkedHashMap<>();
        source.forEach((key, value) -> result.put(String.valueOf(key), value));
        return result;
    }

    private static Map<String, String> stringMap(Object raw) {
        if (!(raw instanceof Map<?, ?> map)) {
            return Map.of();
        }
        Map<String, String> result = new LinkedHashMap<>();
        map.forEach((key, value) -> result.put(String.valueOf(key), String.valueOf(value)));
        return result;
    }

    private static List<String> commandList(Map<String, Object> map) {
        Object command = map.get("command");
        List<String> result = new ArrayList<>();
        if (command instanceof List<?> list) {
            list.forEach(value -> result.add(String.valueOf(value)));
        } else if (command != null) {
            result.add(String.valueOf(command));
        }
        Object args = map.get("args");
        if (args instanceof List<?> list) {
            list.forEach(value -> result.add(String.valueOf(value)));
        }
        return List.copyOf(result);
    }

    private static String safeReason(Exception exception) {
        String message = exception.getMessage();
        return message == null || message.isBlank()
                ? exception.getClass().getSimpleName()
                : message.substring(0, Math.min(message.length(), 240));
    }

    private static void closeQuietly(String serverId, McpClient client) {
        try {
            client.close();
        } catch (Exception e) {
            log.debug("Error closing MCP client {}: {}", serverId, e.getMessage());
        }
    }

    private record Activation(boolean requested, String reason) {}

    public record McpServerEntry(
            String transport,
            String url,
            List<String> command,
            Map<String, String> environment,
            Map<String, String> headers,
            String description
    ) {
        /** Compatibility constructor for the existing REST request shape. */
        public McpServerEntry(
                String transport,
                String url,
                List<String> command,
                Map<String, String> environment,
                String description
        ) {
            this(transport, url, command, environment, Map.of(), description);
        }

        public static McpServerEntry fromMap(Map<String, Object> map) {
            return new McpServerEntry(
                    String.valueOf(map.getOrDefault("transport", "streamable-http")),
                    map.get("url") == null ? null : String.valueOf(map.get("url")),
                    commandList(map),
                    stringMap(map.containsKey("environment") ? map.get("environment") : map.get("env")),
                    stringMap(map.get("headers")),
                    String.valueOf(map.getOrDefault("description", ""))
            );
        }

        public Map<String, Object> toMap() {
            Map<String, Object> map = new LinkedHashMap<>();
            map.put("transport", transport);
            if (url != null) map.put("url", url);
            if (command != null && !command.isEmpty()) map.put("command", command);
            if (environment != null && !environment.isEmpty()) map.put("environment", environment);
            if (headers != null && !headers.isEmpty()) map.put("headers", headers);
            if (description != null && !description.isEmpty()) map.put("description", description);
            return map;
        }
    }
}
