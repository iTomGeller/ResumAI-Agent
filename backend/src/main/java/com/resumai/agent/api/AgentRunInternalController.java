package com.resumai.agent.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.ContextSnapshotMapper;
import com.resumai.agent.domain.entity.ContextSnapshotRow;
import com.resumai.agent.service.InternalWorkflowService;
import com.resumai.agent.service.LlmInvocationService;
import com.resumai.agent.service.MemoryService;
import com.resumai.agent.service.RunMemoryUsageService;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunSchedulerService;
import java.time.Instant;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/**
 * Docker-internal control plane for the Python agent runtime: run events,
 * final results, layered memory access and context snapshots. Protected by the
 * shared internal token.
 */
@RestController
@RequestMapping("/api/internal/agent-runs")
public class AgentRunInternalController {

    private final InternalWorkflowService internalWorkflowService;
    private final RunLifecycleService lifecycleService;
    private final RunSchedulerService schedulerService;
    private final MemoryService memoryService;
    private final RunMemoryUsageService runMemoryUsageService;
    private final LlmInvocationService llmInvocationService;
    private final ContextSnapshotMapper contextSnapshotMapper;
    private final ObjectMapper objectMapper;

    public AgentRunInternalController(InternalWorkflowService internalWorkflowService,
                                      RunLifecycleService lifecycleService,
                                      RunSchedulerService schedulerService,
                                      MemoryService memoryService,
                                      RunMemoryUsageService runMemoryUsageService,
                                      LlmInvocationService llmInvocationService,
                                      ContextSnapshotMapper contextSnapshotMapper,
                                      ObjectMapper objectMapper) {
        this.internalWorkflowService = internalWorkflowService;
        this.lifecycleService = lifecycleService;
        this.schedulerService = schedulerService;
        this.memoryService = memoryService;
        this.runMemoryUsageService = runMemoryUsageService;
        this.llmInvocationService = llmInvocationService;
        this.contextSnapshotMapper = contextSnapshotMapper;
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

    @PostMapping("/{runId}/execution-permit/release")
    public Map<String, String> releaseExecutionPermit(
            @RequestHeader("X-Internal-Token") String token,
            @PathVariable String runId) {
        authorize(token);
        lifecycleService.releaseExecutionPermit(runId);
        return Map.of("status", "RELEASED");
    }

    @PostMapping("/{runId}/execution-permit/acquire")
    public Map<String, String> acquireExecutionPermit(
            @RequestHeader("X-Internal-Token") String token,
            @PathVariable String runId) {
        authorize(token);
        if (!lifecycleService.tryAcquireExecutionPermit(runId)) {
            throw new ResponseStatusException(
                    HttpStatus.TOO_MANY_REQUESTS, "workflow execution capacity full");
        }
        return Map.of("status", "ACQUIRED");
    }

    public record RuntimeLlmInvocationRequest(
            String runId, String traceId, String spanId, String modelName,
            String agentRole, String purpose, Long durationMs,
            String prompt, String response, Integer inputTokens,
            Integer outputTokens, String finishReason, String errorCode,
            String errorBody) {
    }

    /**
     * Durable Context Audit sink for Python workflow LLM calls. Full prompts
     * are intentionally stored outside run_event so they are never replayed
     * through the public SSE trace. LlmInvocationService applies PII/secret
     * redaction before persistence.
     */
    @PostMapping("/llm-invocations")
    public Map<String, String> ingestLlmInvocation(
            @RequestHeader("X-Internal-Token") String token,
            @RequestBody RuntimeLlmInvocationRequest request) {
        authorize(token);
        if (!hasText(request.runId()) || !hasText(request.agentRole())) {
            throw new ResponseStatusException(
                    HttpStatus.BAD_REQUEST, "runId and agentRole required");
        }
        var saved = llmInvocationService.saveInvocation(
                request.traceId(), request.spanId(), request.modelName(),
                request.agentRole(), request.purpose(),
                Math.max(0L, request.durationMs() != null ? request.durationMs() : 0L),
                request.prompt(), request.response(), request.inputTokens(),
                request.outputTokens(), request.finishReason(),
                request.errorCode(), request.errorBody());
        return Map.of("status", "OK", "invocationId", saved.getId());
    }

    public record RuntimeResultRequest(String runId, String status, String answer,
                                       String errorCode, String errorMessage,
                                       Map<String, Object> sharedState,
                                       Map<String, Object> metrics,
                                       Map<String, Object> promptVersions,
                                       Map<String, Object> skillVersions,
                                       String conversationSummary,
                                       String currentGoal,
                                       Map<String, Object> executionSnapshot,
                                       Map<String, Object> structuredReport) {
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
                        request.conversationSummary(), request.currentGoal(),
                        request.executionSnapshot(), request.structuredReport()));
        schedulerService.kick();
        return Map.of("status", "OK", "accepted", accepted);
    }

    public record CheckpointRequest(String runId, Map<String, Object> executionSnapshot) {
    }

    /** Group-boundary checkpoint used by failed-run retry (never on terminal runs). */
    @PostMapping("/{runId}/checkpoint")
    public Map<String, Object> saveCheckpoint(@RequestHeader("X-Internal-Token") String token,
                                              @org.springframework.web.bind.annotation.PathVariable String runId,
                                              @RequestBody CheckpointRequest request) {
        authorize(token);
        boolean saved = lifecycleService.saveRunCheckpoint(runId, request.executionSnapshot());
        return Map.of("status", "OK", "saved", saved);
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
        throw new ResponseStatusException(
                HttpStatus.GONE,
                "pre-terminal memory writes are disabled; use terminal memory candidates");
    }

    public record MemorySearchRequest(String query, List<String> types, String userId,
                                      String conversationId, String runId, Integer topK,
                                      Double minConfidence, String channel,
                                      String consumerAgent, String consumerVersion,
                                      Boolean includeBenchmarkSources,
                                      String jobCategory, String jdFingerprint) {
    }

    @PostMapping("/memory/search")
    public Map<String, Object> memorySearch(@RequestHeader("X-Internal-Token") String token,
                                            @RequestBody MemorySearchRequest request) {
        authorize(token);
        long searchStartedNanos = System.nanoTime();
        Instant searchStartedAt = Instant.now();
        List<Map<String, Object>> hits = memoryService.search(new MemoryService.SearchRequest(
                request.query(), request.types(), request.userId(), request.conversationId(),
                request.runId(), request.topK(), request.minConfidence(), false,
                request.channel(), request.consumerAgent(),
                request.includeBenchmarkSources(), request.consumerVersion(),
                request.jobCategory(), request.jdFingerprint()));
        Instant searchEndedAt = Instant.now();
        long searchDurationMs = Math.max(
                0L, (System.nanoTime() - searchStartedNanos) / 1_000_000L);
        String occurredAt = searchEndedAt.toString();
        Map<String, Object> readAudit = new LinkedHashMap<>();
        readAudit.put("memoryId", "");
        readAudit.put("type", "MULTI");
        readAudit.put("memoryType", "MULTI");
        readAudit.put("taxonomy", "MULTI");
        readAudit.put("namespace", scopedNamespace(request));
        readAudit.put("agent", defaultAgent(request.consumerAgent()));
        readAudit.put("runId", request.runId());
        readAudit.put("reason", "agent_memory_retrieval");
        readAudit.put("hitCount", hits.size());
        readAudit.put("consumerVersion", request.consumerVersion());
        readAudit.put("startedAt", searchStartedAt.toString());
        readAudit.put("endedAt", searchEndedAt.toString());
        readAudit.put("durationMs", searchDurationMs);
        readAudit.put("occurredAt", occurredAt);
        if (hasText(request.runId())) {
            lifecycleService.applyRuntimeEvent(request.runId(), "memory.read",
                    defaultAgent(request.consumerAgent()), "memory_search", readAudit);
            if (hits.isEmpty()) {
                Map<String, Object> missAudit = new LinkedHashMap<>(readAudit);
                missAudit.put("reason", "no_relevant_memory_after_scope_confidence_expiry_filters");
                lifecycleService.applyRuntimeEvent(request.runId(), "memory.missed",
                        defaultAgent(request.consumerAgent()), "memory_search", missAudit);
            } else {
                for (Map<String, Object> hit : hits) {
                    Map<String, Object> selected = new LinkedHashMap<>();
                    copyAuditField(hit, selected, "memoryId");
                    copyAuditField(hit, selected, "type");
                    copyAuditField(hit, selected, "memoryType");
                    copyAuditField(hit, selected, "taxonomy");
                    copyAuditField(hit, selected, "namespace");
                    copyAuditField(hit, selected, "scope");
                    copyAuditField(hit, selected, "source");
                    copyAuditField(hit, selected, "score");
                    selected.put("agent", defaultAgent(request.consumerAgent()));
                    selected.put("runId", request.runId());
                    selected.put("reason", hit.getOrDefault(
                            "selectionReason", "ranked_after_scope_confidence_expiry_filters"));
                    selected.put("occurredAt", hit.getOrDefault("occurredAt", occurredAt));
                    lifecycleService.applyRuntimeEvent(request.runId(), "memory.selected",
                            defaultAgent(request.consumerAgent()), "memory_search", selected);
                }
            }
        }
        return Map.of("hits", hits, "hitCount", hits.size());
    }

    private static void copyAuditField(Map<String, Object> source,
                                       Map<String, Object> target, String key) {
        Object value = source.get(key);
        if (value != null) {
            target.put(key, value);
        }
    }

    private static String defaultAgent(String consumerAgent) {
        return hasText(consumerAgent)
                ? consumerAgent : "MemoryConsumer";
    }

    private static boolean hasText(String value) {
        return value != null && !value.isBlank();
    }

    private static String scopedNamespace(MemorySearchRequest request) {
        if (request.runId() != null && !request.runId().isBlank()) {
            return "run";
        }
        if (request.conversationId() != null && !request.conversationId().isBlank()) {
            return "conversation";
        }
        if (request.userId() != null && !request.userId().isBlank()) {
            return "user";
        }
        return "global";
    }

    /**
     * Persist USED/IGNORED memory decisions after agent retrieval for Ops drilldown.
     */
    @PostMapping("/{runId}/memory-usage")
    public Map<String, Object> memoryUsage(@RequestHeader("X-Internal-Token") String token,
                                           @PathVariable String runId,
                                           @RequestBody Map<String, Object> body) {
        authorize(token);
        int written = runMemoryUsageService.recordUsageFromPayload(runId, body);
        return Map.of("status", "OK", "written", written);
    }

    // ------------------------------------------------------------------
    // Context snapshots
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

    // ------------------------------------------------------------------
    // Deploy drain / recovery control plane
    // ------------------------------------------------------------------

    /** Deploy drain: stop dispatching new QUEUED→STARTING transitions. */
    @PostMapping("/drain")
    public Map<String, Object> beginDrain(@RequestHeader("X-Internal-Token") String token,
                                         @RequestBody(required = false) Map<String, Object> body) {
        authorize(token);
        boolean enabled = body == null || !Boolean.FALSE.equals(body.get("enabled"));
        schedulerService.setDraining(enabled);
        Map<String, Object> snap = new java.util.LinkedHashMap<>(
                schedulerService.activeRunsSnapshot());
        snap.put("status", enabled ? "DRAINING" : "DISPATCHING");
        return snap;
    }

    /** Resume dispatch after deploy readiness. */
    @PostMapping("/resume-dispatch")
    public Map<String, Object> resumeDispatch(@RequestHeader("X-Internal-Token") String token) {
        authorize(token);
        schedulerService.setDraining(false);
        schedulerService.kick();
        Map<String, Object> snap = new java.util.LinkedHashMap<>(
                schedulerService.activeRunsSnapshot());
        snap.put("status", "DISPATCHING");
        return snap;
    }

    /** Active-run snapshot for the safe-deploy wait loop. */
    @GetMapping("/active")
    public Map<String, Object> activeRuns(@RequestHeader("X-Internal-Token") String token) {
        authorize(token);
        return schedulerService.activeRunsSnapshot();
    }

    private String trim(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() > max ? text.substring(0, max) : text;
    }
}
