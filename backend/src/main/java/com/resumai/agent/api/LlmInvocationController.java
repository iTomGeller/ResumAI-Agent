package com.resumai.agent.api;

import com.resumai.agent.api.dto.LlmInvocationResponse;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.service.LlmInvocationService;import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/llm-invocations")
public class LlmInvocationController {

    private final LlmInvocationService llmInvocationService;

    public LlmInvocationController(LlmInvocationService llmInvocationService) {
        this.llmInvocationService = llmInvocationService;
    }

    @GetMapping
    public PageResult<LlmInvocationResponse> listInvocations(
            @RequestParam(required = false) String traceId,
            @RequestParam(required = false) String agentRole,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        return llmInvocationService.queryInvocations(traceId, agentRole, page, pageSize);
    }
    @GetMapping("/{id}")
    public LlmInvocationResponse getInvocation(@PathVariable String id) {
        return llmInvocationService.getInvocation(id);
    }
}
