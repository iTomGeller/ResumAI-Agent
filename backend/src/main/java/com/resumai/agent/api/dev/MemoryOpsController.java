package com.resumai.agent.api.dev;

import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryOpsResponse;
import com.resumai.agent.service.ops.OpsDebugService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/dev/memory")
public class MemoryOpsController {

    private final OpsDebugService opsDebugService;

    public MemoryOpsController(OpsDebugService opsDebugService) {
        this.opsDebugService = opsDebugService;
    }

    @GetMapping
    public MemoryOpsResponse memory(
            @RequestParam(defaultValue = "50") int limit,
            @RequestParam(required = false) String scope,
            @RequestParam(required = false) String source,
            @RequestParam(required = false) String runId,
            @RequestParam(required = false) String decision,
            @RequestParam(defaultValue = "false") boolean includeBenchmark,
            @RequestParam(defaultValue = "false") boolean includeControlFailure) {
        return opsDebugService.memory(limit, scope, source, runId, decision,
                includeBenchmark, includeControlFailure);
    }
}
