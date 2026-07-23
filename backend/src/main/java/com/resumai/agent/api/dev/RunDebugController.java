package com.resumai.agent.api.dev;

import com.resumai.agent.api.ApiNotFoundException;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugDetailResponse;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.RunDebugSummary;
import com.resumai.agent.service.ops.OpsDebugService;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/dev/runs")
public class RunDebugController {

    private final OpsDebugService opsDebugService;

    public RunDebugController(OpsDebugService opsDebugService) {
        this.opsDebugService = opsDebugService;
    }

    @GetMapping
    public Map<String, Object> list(@RequestParam(required = false) String traceId,
                                    @RequestParam(required = false) String runId,
                                    @RequestParam(required = false) String conversationId,
                                    @RequestParam(required = false) String status,
                                    @RequestParam(defaultValue = "40") int limit) {
        List<RunDebugSummary> items = opsDebugService.listRuns(
                traceId, runId, conversationId, status, limit);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", items.size());
        body.put("items", items);
        return body;
    }

    @GetMapping("/{runId}")
    public RunDebugDetailResponse detail(@PathVariable String runId,
                                         @RequestParam(defaultValue = "120") int eventLimit) {
        RunDebugDetailResponse detail = opsDebugService.runDetail(runId, eventLimit, null);
        if (detail == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        return detail;
    }

    @GetMapping("/{runId}/timeline")
    public Map<String, Object> timeline(@PathVariable String runId,
                                        @RequestParam(required = false) Long afterSeq,
                                        @RequestParam(defaultValue = "120") int eventLimit) {
        RunDebugDetailResponse detail = opsDebugService.runDetail(runId, eventLimit, afterSeq);
        if (detail == null) {
            throw new ApiNotFoundException("Run 不存在：" + runId);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("runId", runId);
        body.put("timeline", detail.timeline());
        body.put("truncated", detail.truncated());
        body.put("nextSeq", detail.nextSeq());
        return body;
    }

    @GetMapping("/{runId}/tree")
    public Map<String, Object> tree(@PathVariable String runId) {
        Map<String, Object> tree = opsDebugService.executionTree(runId);
        if (tree.get("run") == null && ((List<?>) tree.getOrDefault("nodes", List.of())).isEmpty()
                && tree.get("plan") == null) {
            // still return empty shell when run missing — keep contract stable
        }
        return tree;
    }
}
