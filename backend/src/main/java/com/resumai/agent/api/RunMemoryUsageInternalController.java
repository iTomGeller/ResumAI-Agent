package com.resumai.agent.api;

import com.resumai.agent.service.InternalWorkflowService;
import com.resumai.agent.service.RunMemoryUsageService;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/** Lightweight internal callback for run memory USED/IGNORED decisions. */
@RestController
@RequestMapping("/api/internal/runs")
public class RunMemoryUsageInternalController {

    private final InternalWorkflowService internalWorkflowService;
    private final RunMemoryUsageService runMemoryUsageService;

    public RunMemoryUsageInternalController(InternalWorkflowService internalWorkflowService,
                                            RunMemoryUsageService runMemoryUsageService) {
        this.internalWorkflowService = internalWorkflowService;
        this.runMemoryUsageService = runMemoryUsageService;
    }

    @PostMapping("/{runId}/memory-usage")
    public Map<String, Object> memoryUsage(@RequestHeader("X-Internal-Token") String token,
                                           @PathVariable String runId,
                                           @RequestBody Map<String, Object> body) {
        if (!internalWorkflowService.authorize(token)) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "invalid internal token");
        }
        int written = runMemoryUsageService.recordUsageFromPayload(runId, body);
        return Map.of("status", "OK", "written", written);
    }
}
