package com.resumai.agent.mcp;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Loads the shared {@code mcp-servers.json} used by backend + workflow.
 * Resolution order: {@code app.mcp.config-path} / {@code MCP_CONFIG_PATH} →
 * {@code /app/config/mcp-servers.json} → classpath {@code mcp-servers.json}.
 */
@Component
public class McpServersConfig {

    private static final Logger log = LoggerFactory.getLogger(McpServersConfig.class);

    private final ObjectMapper objectMapper = new ObjectMapper();

    @Value("${app.mcp.config-path:/app/config/mcp-servers.json}")
    private String configPath;

    private volatile JsonNode root = objectMapper.createObjectNode();
    private volatile String resolvedPath = "";

    @PostConstruct
    public void load() {
        Path configured = Path.of(configPath == null ? "" : configPath);
        List<Path> candidates = new ArrayList<>();
        if (!configured.toString().isBlank()) {
            candidates.add(configured);
        }
        candidates.add(Path.of("/app/config/mcp-servers.json"));
        candidates.add(Path.of("config/mcp-servers.json"));
        candidates.add(Path.of("mcp-servers.json"));

        for (Path candidate : candidates) {
            if (Files.isRegularFile(candidate)) {
                try {
                    root = objectMapper.readTree(Files.readString(candidate));
                    resolvedPath = candidate.toAbsolutePath().toString();
                    log.info("Loaded MCP config from file {}", resolvedPath);
                    return;
                } catch (IOException e) {
                    log.warn("Failed reading MCP config {}: {}", candidate, e.getMessage());
                }
            }
        }

        try (InputStream in = new ClassPathResource("mcp-servers.json").getInputStream()) {
            root = objectMapper.readTree(in);
            resolvedPath = "classpath:mcp-servers.json";
            log.info("Loaded MCP config from classpath fallback");
        } catch (IOException e) {
            log.warn("MCP config unavailable: {}", e.getMessage());
            root = objectMapper.createObjectNode();
            resolvedPath = "";
        }
    }

    public String getResolvedPath() {
        return resolvedPath;
    }

    public JsonNode getRoot() {
        return root;
    }

    public Map<String, Object> statusSummary() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("configPath", resolvedPath);
        out.put("servers", serverNames("mcpServers"));
        out.put("optionalServers", serverNames("optionalMcpServers"));
        out.put("authenticationMode", "KEYLESS_ONLY");
        return out;
    }

    private List<String> serverNames(String field) {
        List<String> names = new ArrayList<>();
        JsonNode node = root.path(field);
        if (node.isObject()) {
            node.fieldNames().forEachRemaining(names::add);
        }
        return names;
    }
}
