package com.resumai.agent.conversation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.mcp.McpServersConfig;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * Minimal, Copilot-owned MCP client.
 *
 * <p>Only streamable-http servers routed to {@code Copilot} are discovered.
 * The Java Copilot does not call the Python workflow registry and never enters
 * an evaluation run. Tool names and schemas always come from live tools/list.</p>
 */
@Component
public class CopilotMcpClient {

    private static final Logger log = LoggerFactory.getLogger(CopilotMcpClient.class);
    private static final List<String> PROTOCOL_VERSIONS = List.of(
            "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05");
    private static final Duration CATALOG_TTL = Duration.ofMinutes(10);

    public record Tool(String alias, String catalogName, String server,
                       String remoteName, String description,
                       Map<String, Object> inputSchema) {
    }

    private static final class Session {
        private final String server;
        private final String url;
        private final int timeoutSeconds;
        private final Map<String, String> configuredHeaders;
        private final AtomicLong requestId = new AtomicLong();
        private volatile String sessionId;
        private volatile String protocolVersion = PROTOCOL_VERSIONS.getFirst();
        private volatile boolean initialized;

        private Session(String server, String url, int timeoutSeconds,
                        Map<String, String> configuredHeaders) {
            this.server = server;
            this.url = url;
            this.timeoutSeconds = timeoutSeconds;
            this.configuredHeaders = configuredHeaders;
        }
    }

    private final McpServersConfig config;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;
    private final Map<String, Session> sessions = new ConcurrentHashMap<>();
    private volatile List<Tool> cachedTools = List.of();
    private volatile long cachedAtMillis;

    public CopilotMcpClient(McpServersConfig config, ObjectMapper objectMapper) {
        this.config = config;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
    }

    /** Returns provider-native tools[] entries backed by live MCP discovery. */
    public List<Map<String, Object>> providerTools() {
        List<Tool> tools = tools();
        List<Map<String, Object>> result = new ArrayList<>();
        for (Tool tool : tools) {
            result.add(Map.of(
                    "type", "function",
                    "function", Map.of(
                            "name", tool.alias(),
                            "description", tool.description(),
                            "parameters", tool.inputSchema())));
        }
        return result;
    }

    public Map<String, Object> call(String alias, Map<String, Object> arguments) {
        Tool tool = tools().stream()
                .filter(item -> item.alias().equals(alias))
                .findFirst()
                .orElse(null);
        if (tool == null) {
            return Map.of("success", false, "status", "UNAVAILABLE",
                    "text", "MCP tool is not in the live Copilot catalog: " + alias);
        }
        Session session = sessions.get(tool.server());
        if (session == null) {
            invalidate();
            return Map.of("success", false, "status", "UNAVAILABLE",
                    "text", "MCP server session is unavailable: " + tool.server());
        }
        try {
            Map<String, Object> result = rpc(session, "tools/call", Map.of(
                    "name", tool.remoteName(),
                    "arguments", arguments != null ? arguments : Map.of()));
            List<String> texts = new ArrayList<>();
            Object content = result.get("content");
            if (content instanceof List<?> list) {
                for (Object item : list) {
                    if (item instanceof Map<?, ?> map
                            && "text".equals(String.valueOf(map.get("type")))) {
                        texts.add(String.valueOf(
                                map.get("text") != null ? map.get("text") : ""));
                    }
                }
            }
            Map<String, Object> normalized = new LinkedHashMap<>();
            normalized.put("success", !Boolean.TRUE.equals(result.get("isError")));
            normalized.put("status", Boolean.TRUE.equals(result.get("isError"))
                    ? "ERROR" : "SUCCESS");
            normalized.put("tool", tool.catalogName());
            normalized.put("mcpServer", tool.server());
            normalized.put("text", clipPreservingHeadTail(String.join("\n", texts), 12000));
            if (result.get("structuredContent") instanceof Map<?, ?> structured) {
                normalized.put("structuredContent", structured);
            }
            return normalized;
        } catch (Exception e) {
            session.initialized = false;
            log.info("Copilot MCP call failed tool={}: {}", tool.catalogName(), e.getMessage());
            return Map.of("success", false, "status", "UNAVAILABLE",
                    "tool", tool.catalogName(), "mcpServer", tool.server(),
                    "text", clip(e.getMessage(), 500));
        }
    }

    public synchronized void invalidate() {
        cachedAtMillis = 0L;
        cachedTools = List.of();
    }

    private List<Tool> tools() {
        long now = System.currentTimeMillis();
        if (!cachedTools.isEmpty() && now - cachedAtMillis < CATALOG_TTL.toMillis()) {
            return cachedTools;
        }
        synchronized (this) {
            now = System.currentTimeMillis();
            if (!cachedTools.isEmpty() && now - cachedAtMillis < CATALOG_TTL.toMillis()) {
                return cachedTools;
            }
            cachedTools = discover();
            cachedAtMillis = now;
            return cachedTools;
        }
    }

    private List<Tool> discover() {
        JsonNode root = config.getRoot();
        JsonNode routing = root.path("agentToolRouting").path("Copilot");
        if (!routing.isArray()) {
            return List.of();
        }
        Set<String> routed = new java.util.LinkedHashSet<>();
        routing.forEach(item -> routed.add(item.asText()));
        Map<String, JsonNode> serverConfigs = new LinkedHashMap<>();
        collectServerConfigs(root.path("mcpServers"), serverConfigs);
        collectServerConfigs(root.path("optionalMcpServers"), serverConfigs);

        List<Tool> discovered = new ArrayList<>();
        for (Map.Entry<String, JsonNode> entry : serverConfigs.entrySet()) {
            String serverName = entry.getKey();
            JsonNode serverConfig = entry.getValue();
            if (!serverConfig.path("enabled").asBoolean(true)
                    || !"streamable-http".equals(serverConfig.path("transport").asText())
                    || !StringUtils.hasText(serverConfig.path("url").asText())) {
                continue;
            }
            boolean routedServer = routed.stream().anyMatch(
                    name -> name.startsWith(serverName + "."));
            if (!routedServer) {
                continue;
            }
            try {
                Session session = sessions.computeIfAbsent(serverName,
                        ignored -> new Session(
                                serverName,
                                expandEnv(serverConfig.path("url").asText()),
                                Math.max(1, serverConfig.path("timeoutSeconds").asInt(20)),
                                stringMap(serverConfig.path("headers"))));
                initialize(session);
                Map<String, Object> listed = rpc(session, "tools/list", Map.of());
                Object rawTools = listed.get("tools");
                if (!(rawTools instanceof List<?> list)) {
                    continue;
                }
                Set<String> allowed = new java.util.LinkedHashSet<>();
                serverConfig.path("allowedTools").forEach(item -> allowed.add(item.asText()));
                String prefix = serverConfig.path("toolPrefix").asText(serverName);
                String guidance = serverConfig.path("routingGuidance").asText("");
                for (Object raw : list) {
                    if (!(raw instanceof Map<?, ?> map)) {
                        continue;
                    }
                    String remoteName = String.valueOf(
                            map.get("name") != null ? map.get("name") : "");
                    String catalogName = prefix + "." + remoteName;
                    if (!StringUtils.hasText(remoteName)
                            || (!allowed.isEmpty() && !allowed.contains(remoteName))
                            || !routed.contains(catalogName)) {
                        continue;
                    }
                    String alias = catalogName.replaceAll("[^A-Za-z0-9_-]", "__");
                    String description = String.valueOf(map.get("description") != null
                            ? map.get("description") : "MCP " + catalogName);
                    if (StringUtils.hasText(guidance)) {
                        description = description + " Runtime routing guidance: " + guidance;
                    }
                    Map<String, Object> schema = map.get("inputSchema") instanceof Map<?, ?> value
                            ? castMap(value) : Map.of("type", "object");
                    discovered.add(new Tool(alias, catalogName, serverName,
                            remoteName, description, schema));
                }
            } catch (Exception e) {
                sessions.remove(serverName);
                log.info("Copilot MCP discovery skipped server={}: {}", serverName, e.getMessage());
            }
        }
        return List.copyOf(discovered);
    }

    private void initialize(Session session) throws IOException, InterruptedException {
        if (session.initialized) {
            return;
        }
        synchronized (session) {
            if (session.initialized) {
                return;
            }
            Exception last = null;
            for (String version : PROTOCOL_VERSIONS) {
                try {
                    session.protocolVersion = version;
                    Map<String, Object> result = rpcRaw(session, "initialize", Map.of(
                            "protocolVersion", version,
                            "capabilities", Map.of(),
                            "clientInfo", Map.of(
                                    "name", "resumai-copilot", "version", "1.0")), false);
                    Object negotiated = result.get("protocolVersion");
                    if (negotiated != null) {
                        session.protocolVersion = String.valueOf(negotiated);
                    }
                    notifyInitialized(session);
                    session.initialized = true;
                    return;
                } catch (IOException e) {
                    last = e;
                }
            }
            throw new IOException("MCP initialize failed for " + session.server,
                    last);
        }
    }

    private void notifyInitialized(Session session) throws IOException, InterruptedException {
        Map<String, Object> payload = Map.of(
                "jsonrpc", "2.0",
                "method", "notifications/initialized",
                "params", Map.of());
        post(session, payload, true);
    }

    private Map<String, Object> rpc(Session session, String method,
                                    Map<String, Object> params)
            throws IOException, InterruptedException {
        initialize(session);
        return rpcRaw(session, method, params, true);
    }

    private Map<String, Object> rpcRaw(Session session, String method,
                                       Map<String, Object> params,
                                       boolean includeProtocol)
            throws IOException, InterruptedException {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("jsonrpc", "2.0");
        payload.put("id", session.requestId.incrementAndGet());
        payload.put("method", method);
        payload.put("params", params);
        Map<String, Object> envelope = post(session, payload, includeProtocol);
        if (envelope.get("error") != null) {
            throw new IOException("MCP " + session.server + " " + envelope.get("error"));
        }
        return envelope.get("result") instanceof Map<?, ?> result
                ? castMap(result) : envelope;
    }

    private Map<String, Object> post(Session session, Map<String, Object> payload,
                                     boolean includeProtocol)
            throws IOException, InterruptedException {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(session.url))
                .timeout(Duration.ofSeconds(session.timeoutSeconds))
                .header("Content-Type", "application/json")
                .header("Accept", "application/json, text/event-stream");
        session.configuredHeaders.forEach(builder::header);
        if (includeProtocol) {
            builder.header("MCP-Protocol-Version", session.protocolVersion);
        }
        if (StringUtils.hasText(session.sessionId)) {
            builder.header("Mcp-Session-Id", session.sessionId);
        }
        HttpResponse<String> response = httpClient.send(
                builder.POST(HttpRequest.BodyPublishers.ofString(
                        objectMapper.writeValueAsString(payload))).build(),
                HttpResponse.BodyHandlers.ofString());
        response.headers().firstValue("mcp-session-id")
                .ifPresent(value -> session.sessionId = value);
        if (response.statusCode() >= 400) {
            throw new IOException("HTTP " + response.statusCode() + ": "
                    + clip(response.body(), 300));
        }
        return parseResponse(response.body(), response.headers()
                .firstValue("content-type").orElse(""));
    }

    private Map<String, Object> parseResponse(String body, String contentType)
            throws IOException {
        if (!StringUtils.hasText(body)) {
            return Map.of();
        }
        if (!contentType.toLowerCase().contains("text/event-stream")
                && !body.stripLeading().startsWith("event:")) {
            return objectMapper.readValue(body, Map.class);
        }
        Map<String, Object> last = Map.of();
        StringBuilder data = new StringBuilder();
        for (String line : body.split("\\R", -1)) {
            if (line.startsWith("data:")) {
                if (!data.isEmpty()) {
                    data.append('\n');
                }
                data.append(line.substring(5).stripLeading());
            } else if (line.isBlank() && !data.isEmpty()) {
                last = objectMapper.readValue(data.toString(), Map.class);
                data.setLength(0);
            }
        }
        if (!data.isEmpty()) {
            last = objectMapper.readValue(data.toString(), Map.class);
        }
        return last;
    }

    private static void collectServerConfigs(JsonNode node,
                                             Map<String, JsonNode> target) {
        if (!node.isObject()) {
            return;
        }
        Iterator<Map.Entry<String, JsonNode>> fields = node.fields();
        fields.forEachRemaining(entry -> target.put(entry.getKey(), entry.getValue()));
    }

    private static Map<String, String> stringMap(JsonNode node) {
        Map<String, String> result = new LinkedHashMap<>();
        if (node.isObject()) {
            node.fields().forEachRemaining(entry ->
                    result.put(entry.getKey(), expandEnv(entry.getValue().asText())));
        }
        return result;
    }

    private static String expandEnv(String value) {
        if (value == null) {
            return "";
        }
        java.util.regex.Matcher matcher = java.util.regex.Pattern
                .compile("\\$\\{([A-Z0-9_]+)}").matcher(value);
        StringBuffer out = new StringBuffer();
        while (matcher.find()) {
            matcher.appendReplacement(out, java.util.regex.Matcher.quoteReplacement(
                    System.getenv().getOrDefault(matcher.group(1), "")));
        }
        matcher.appendTail(out);
        return out.toString();
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> castMap(Map<?, ?> value) {
        return (Map<String, Object>) value;
    }

    private static String clip(String value, int limit) {
        String text = value == null ? "" : value;
        return text.length() <= limit ? text : text.substring(0, limit);
    }

    private static String clipPreservingHeadTail(String value, int limit) {
        String text = value == null ? "" : value;
        if (text.length() <= limit) {
            return text;
        }
        String marker = "\n[…中间内容已截断…]\n";
        int available = Math.max(2, limit - marker.length());
        int head = Math.max(1, (int) Math.ceil(available * 0.6));
        int tail = Math.max(1, available - head);
        return text.substring(0, head) + marker
                + text.substring(text.length() - tail);
    }
}
