package com.resumai.agent.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.ContextSnapshotMapper;
import com.resumai.agent.dao.SandboxExecutionMapper;
import com.resumai.agent.domain.entity.ContextSnapshotRow;
import com.resumai.agent.domain.entity.SandboxExecutionRow;
import com.resumai.agent.service.InternalWorkflowService;
import com.resumai.agent.service.MemoryService;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunSchedulerService;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/**
 * Docker-internal control plane for the Python agent runtime: run events,
 * final results, layered memory access, context snapshots and sandbox
 * execution records. Protected by the shared internal token.
 */
@RestController
@RequestMapping("/api/internal/agent-runs")
public class AgentRunInternalController {

    private final InternalWorkflowService internalWorkflowService;
    private final RunLifecycleService lifecycleService;
    private final RunSchedulerService schedulerService;
    private final MemoryService memoryService;
    private final ContextSnapshotMapper contextSnapshotMapper;
    private final SandboxExecutionMapper sandboxExecutionMapper;
    private final ObjectMapper objectMapper;

    public AgentRunInternalController(InternalWorkflowService internalWorkflowService,
                                      RunLifecycleService lifecycleService,
                                      RunSchedulerService schedulerService,
                                      MemoryService memoryService,
                                      ContextSnapshotMapper contextSnapshotMapper,
                                      SandboxExecutionMapper sandboxExecutionMapper,
                                      ObjectMapper objectMapper) {
        this.internalWorkflowService = internalWorkflowService;
        this.lifecycleService = lifecycleService;
        this.schedulerService = schedulerService;
        this.memoryService = memoryService;
        this.contextSnapshotMapper = contextSnapshotMapper;
        this.sandboxExecutionMapper = sandboxExecutionMapper;
        this.objectMapper = objectMapper;
    }

    private void authorize(String token) {
        if (!internalWorkflowService.authorize(token)) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "invalid internal token");
        }
    }

    public record RuntimeEventRequest(String runId, String eventType, String agentId,
                                      String toolName, Map<String, Object> payload) {
    }

    @PostMapping("/events")
    public Map<String, String> ingestEvent(@RequestHeader("X-Internal-Token") String token,
                                           @RequestBody RuntimeEventRequest request) {
        authorize(token);
        if (request.runId() == null || request.eventType() == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "runId and eventType required");
        }
        lifecycleService.applyRuntimeEvent(request.runId(), request.eventType(),
                request.agentId(), request.toolName(),
                request.payload() != null ? request.payload() : Map.of());
        return Map.of("status", "OK");
    }

    public record RuntimeResultRequest(String runId, String status, String answer,
                                       String errorCode, String errorMessage,
                                       Map<String, Object> sharedState,
                                       Map<String, Object> metrics,
                                       Map<String, Object> promptVersions,
                                       Map<String, Object> skillVersions,
                                       String conversationSummary,
                                       String currentGoal) {
    }

    @PostMapping("/result")
    public Map<String, Object> ingestResult(@RequestHeader("X-Internal-Token") String token,
                                            @RequestBody RuntimeResultRequest request) {
        authorize(token);
        boolean accepted = lifecycleService.applyRuntimeResult(request.runId(),
                new RunLifecycleService.RuntimeResult(
                        request.status(), request.answer(), request.errorCode(),
                        request.errorMessage(), request.sharedState(), request.metrics(),
                        request.promptVersions(), request.skillVersions(),
                        request.conversationSummary(), request.currentGoal()));
        schedulerService.kick();
        return Map.of("status", "OK", "accepted", accepted);
    }

    // ------------------------------------------------------------------
    // Layered memory
    // ------------------------------------------------------------------

    public record MemoryWriteRequest(String type, String ownerScope, String userId,
                                     String conversationId, String runId, String content,
                                     Map<String, Object> structuredContent, String source,
                                     String sourceId, Double confidence, String sensitivityLevel,
                                     Integer ttlDays) {
    }

    @PostMapping("/memory/write")
    public Map<String, Object> memoryWrite(@RequestHeader("X-Internal-Token") String token,
                                           @RequestBody MemoryWriteRequest request) {
        authorize(token);
        var row = memoryService.write(new MemoryService.WriteRequest(
                request.type(), request.ownerScope(), request.userId(), request.conversationId(),
                request.runId(), request.content(), request.structuredContent(), request.source(),
                request.sourceId(), request.confidence(), request.sensitivityLevel(),
                request.ttlDays()));
        return Map.of("status", "OK", "memoryId", row.getMemoryId());
    }

    public record MemorySearchRequest(String query, List<String> types, String userId,
                                      String conversationId, String runId, Integer topK,
                                      Double minConfidence) {
    }

    @PostMapping("/memory/search")
    public Map<String, Object> memorySearch(@RequestHeader("X-Internal-Token") String token,
                                            @RequestBody MemorySearchRequest request) {
        authorize(token);
        List<Map<String, Object>> hits = memoryService.search(new MemoryService.SearchRequest(
                request.query(), request.types(), request.userId(), request.conversationId(),
                request.runId(), request.topK(), request.minConfidence(), false));
        return Map.of("hits", hits, "hitCount", hits.size());
    }

    // ------------------------------------------------------------------
    // Context snapshots + sandbox execution records
    // ------------------------------------------------------------------

    public record ContextSnapshotRequest(String runId, String conversationId, Integer summaryVersion,
                                         Long sourceMessageStartId, Long sourceMessageEndId,
                                         Long firstKeptMessageId, Integer beforeTokenEstimate,
                                         Integer afterTokenEstimate, String reason, String summary) {
    }

    @PostMapping("/context-snapshots")
    public Map<String, Object> saveContextSnapshot(@RequestHeader("X-Internal-Token") String token,
                                                   @RequestBody ContextSnapshotRequest request) {
        authorize(token);
        ContextSnapshotRow row = new ContextSnapshotRow();
        row.setRunId(request.runId());
        row.setConversationId(request.conversationId());
        row.setSummaryVersion(request.summaryVersion() != null ? request.summaryVersion() : 1);
        row.setSourceMessageStartId(request.sourceMessageStartId());
        row.setSourceMessageEndId(request.sourceMessageEndId());
        row.setFirstKeptMessageId(request.firstKeptMessageId());
        row.setBeforeTokenEstimate(request.beforeTokenEstimate());
        row.setAfterTokenEstimate(request.afterTokenEstimate());
        row.setReason(request.reason());
        row.setSummary(request.summary());
        row.setCreateTime(LocalDateTime.now());
        contextSnapshotMapper.insert(row);
        return Map.of("status", "OK", "id", row.getId());
    }

    public record SandboxExecutionRequest(String sandboxId, String runId, String conversationId,
                                          String toolName, String containerId, String status,
                                          Integer exitCode, Long durationMs, String stdoutTail,
                                          String stderrTail, String error, String expireAt) {
    }

    @PostMapping("/sandbox-executions")
    public Map<String, Object> saveSandboxExecution(@RequestHeader("X-Internal-Token") String token,
                                                    @RequestBody SandboxExecutionRequest request) {
        authorize(token);
        SandboxExecutionRow existing = sandboxExecutionMapper.selectOne(
                new com.baomidou.mybatisplus.core.conditions.query.QueryWrapper<SandboxExecutionRow>()
                        .eq("sandbox_id", request.sandboxId()).last("limit 1"));
        SandboxExecutionRow row = existing != null ? existing : new SandboxExecutionRow();
        row.setSandboxId(request.sandboxId());
        row.setRunId(request.runId());
        row.setConversationId(request.conversationId());
        row.setToolName(request.toolName());
        row.setContainerId(request.containerId());
        row.setStatus(request.status());
        row.setExitCode(request.exitCode());
        row.setDurationMs(request.durationMs());
        row.setStdoutTail(trim(request.stdoutTail(), 3900));
        row.setStderrTail(trim(request.stderrTail(), 1900));
        row.setError(trim(request.error(), 1900));
        if (request.expireAt() != null && !request.expireAt().isBlank()) {
            try {
                row.setExpireAt(LocalDateTime.parse(request.expireAt().substring(0, 19)));
            } catch (Exception ignored) {
                // best effort — expire_at is advisory metadata
            }
        }
        if (existing == null) {
            row.setCreateTime(LocalDateTime.now());
            sandboxExecutionMapper.insert(row);
        } else {
            if (request.status() != null && !"RUNNING".equals(request.status())) {
                row.setFinishedAt(LocalDateTime.now());
            }
            sandboxExecutionMapper.updateById(row);
        }
        return Map.of("status", "OK");
    }

    private String trim(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() > max ? text.substring(0, max) : text;
    }
}
