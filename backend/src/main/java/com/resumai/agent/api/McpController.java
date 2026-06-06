package com.resumai.agent.api;

import com.resumai.agent.ai.McpToolRegistry;
import com.resumai.agent.ai.McpToolRegistry.McpServerEntry;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

/**
 * REST API for dynamic MCP server management.
 * Input format is compatible with standard mcpServers config (Claude Desktop / Cursor).
 */
@RestController
@RequestMapping("/api/mcp")
public class McpController {

    private final McpToolRegistry mcpRegistry;

    public McpController(McpToolRegistry mcpRegistry) {
        this.mcpRegistry = mcpRegistry;
    }

    @PostMapping("/connect")
    public ResponseEntity<?> connectMcp(@RequestBody McpConnectRequest request) {
        McpServerEntry entry = new McpServerEntry(
                request.transport() != null ? request.transport() : "streamable-http",
                request.url(),
                request.command() != null ? request.command() : List.of(),
                request.environment() != null ? request.environment() : Map.of(),
                request.description() != null ? request.description() : ""
        );
        mcpRegistry.connect(request.serverId(), entry);
        mcpRegistry.persistToConfig(request.serverId(), entry);
        return ResponseEntity.ok(Map.of(
                "status", "connected",
                "serverId", request.serverId(),
                "tools", mcpRegistry.getToolsForServer(request.serverId())
        ));
    }

    @DeleteMapping("/{serverId}")
    public ResponseEntity<?> disconnectMcp(@PathVariable String serverId) {
        mcpRegistry.disconnect(serverId);
        return ResponseEntity.ok(Map.of("status", "disconnected", "serverId", serverId));
    }

    @GetMapping("/servers")
    public ResponseEntity<?> listServers() {
        return ResponseEntity.ok(mcpRegistry.listConnected());
    }

    public record McpConnectRequest(
            String serverId,
            String transport,
            String url,
            List<String> command,
            Map<String, String> environment,
            String description
    ) {}
}
