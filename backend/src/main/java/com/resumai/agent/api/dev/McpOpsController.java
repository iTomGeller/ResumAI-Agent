package com.resumai.agent.api.dev;

import com.resumai.agent.api.dto.ops.OpsDebugDtos.McpOpsResponse;
import com.resumai.agent.service.ops.OpsDebugService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/dev/mcp")
public class McpOpsController {

    private final OpsDebugService opsDebugService;

    public McpOpsController(OpsDebugService opsDebugService) {
        this.opsDebugService = opsDebugService;
    }

    @GetMapping
    public McpOpsResponse inventoryAndInvocations(
            @RequestParam(defaultValue = "false") boolean probe,
            @RequestParam(required = false) String runId,
            @RequestParam(required = false) String server,
            @RequestParam(required = false) String outcome,
            @RequestParam(defaultValue = "40") int recentLimit) {
        return opsDebugService.mcp(probe, runId, server, outcome, recentLimit);
    }
}
