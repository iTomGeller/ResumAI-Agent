package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.WorkflowResultRequest;
import com.resumai.agent.api.dto.WorkflowTraceEventRequest;
import com.resumai.agent.config.AgentMetrics;
import com.resumai.agent.config.WorkflowProperties;
import com.resumai.agent.dao.AgentExecutionTraceMapper;
import com.resumai.agent.domain.entity.AgentExecutionTrace;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Lazy;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class InternalWorkflowService {

    private static final Logger log = LoggerFactory.getLogger(InternalWorkflowService.class);

    private final AgentExecutionTraceMapper agentExecutionTraceMapper;
    private final ResumeEvaluationService resumeEvaluationService;
    private final WorkflowProperties workflowProperties;
    private final ObjectMapper objectMapper;
    private final AgentMetrics agentMetrics;
    private final AgentMemoryService agentMemoryService;

    public InternalWorkflowService(AgentExecutionTraceMapper agentExecutionTraceMapper,
                                   @Lazy ResumeEvaluationService resumeEvaluationService,
                                   WorkflowProperties workflowProperties,
                                   ObjectMapper objectMapper,
                                   AgentMetrics agentMetrics,
                                   AgentMemoryService agentMemoryService) {
        this.agentExecutionTraceMapper = agentExecutionTraceMapper;
        this.resumeEvaluationService = resumeEvaluationService;
        this.workflowProperties = workflowProperties;
        this.objectMapper = objectMapper;
        this.agentMetrics = agentMetrics;
        this.agentMemoryService = agentMemoryService;
    }

    public boolean authorize(String token) {
        return StringUtils.hasText(workflowProperties.getInternalToken())
                && workflowProperties.getInternalToken().equals(token);
    }

    public void upsertTraceEvent(WorkflowTraceEventRequest request) {
        if (!StringUtils.hasText(request.eventId())) {
            throw new IllegalArgumentException("eventId required");
        }
        if (!resumeEvaluationService.acceptsWorkflowCallback(
                request.traceId(), request.workflowRunId(), request.conversationId(), request.revision())) {
            log.info("ignored stale workflow event trace={} run={} revision={} event={}",
                    request.traceId(), request.workflowRunId(), request.revision(), request.eventId());
            return;
        }
        AgentExecutionTrace existing = agentExecutionTraceMapper.selectOne(
                new LambdaQueryWrapper<AgentExecutionTrace>()
                        .eq(AgentExecutionTrace::getEventId, request.eventId())
                        .last("LIMIT 1"));
        LocalDateTime now = LocalDateTime.now();
        AgentExecutionTrace entity = existing != null ? existing : new AgentExecutionTrace();
        if (existing == null) {
            entity.setSpanId("span-" + request.eventId().hashCode());
            entity.setCreateTime(now);
        }
        entity.setTraceId(request.traceId());
        entity.setParentSpanId(shortLegacySpanId(request.parentEventId()));
        entity.setParentEventId(request.parentEventId());
        entity.setAgentRole(request.agentName());
        entity.setEventId(request.eventId());
        entity.setNodeId(request.nodeId());
        entity.setRoundIndex(request.roundIndex());
        entity.setAttempt(request.attempt() != null ? request.attempt() : 1);
        entity.setEventKind(request.kind());
        entity.setCallKind(request.callKind());
        entity.setCallName(request.callName());
        entity.setRoundRole(request.roundRole());
        entity.setParentRoundId(request.parentRoundId());
        entity.setStartedAt(parseWorkflowTime(request.startedAt()));
        entity.setEndedAt(parseWorkflowTime(request.endedAt()));
        entity.setToolCall(mapEventKindToToolCall(request.kind()));
        entity.setModelName(request.modelName());
        entity.setInputSummary(trim(request.inputPreview(), 2000));
        entity.setOutputSummary(trim(request.outputPreview(), 2000));
        entity.setStatus(request.status() != null ? request.status() : "SUCCESS");
        entity.setDurationMs(request.durationMs());
        if (request.tokenUsage() != null && request.tokenUsage().get("total_tokens") instanceof Number n) {
            entity.setCostTokens(n.longValue());
        }
        try {
            if (request.inputMessages() != null) {
                entity.setRawInput(objectMapper.writeValueAsString(request.inputMessages()));
            }
            if (request.outputMessage() != null) {
                entity.setRawOutput(objectMapper.writeValueAsString(request.outputMessage()));
            }
            entity.setPayload(buildPayloadJson(request));
        } catch (Exception e) {
            log.warn("serialize workflow event payload failed: {}", e.getMessage());
        }
        entity.setUpdateTime(now);
        if (existing != null) {
            agentExecutionTraceMapper.updateById(entity);
        } else {
            try {
                agentExecutionTraceMapper.insert(entity);
            } catch (DuplicateKeyException e) {
                AgentExecutionTrace latest = agentExecutionTraceMapper.selectOne(
                        new LambdaQueryWrapper<AgentExecutionTrace>()
                                .eq(AgentExecutionTrace::getEventId, request.eventId())
                                .last("LIMIT 1"));
                if (latest != null) {
                    entity.setId(latest.getId());
                    agentExecutionTraceMapper.updateById(entity);
                } else {
                    throw e;
                }
            }
        }
        recordWorkflowMetrics(request);
        try {
            resumeEvaluationService.publishWorkflowTraceEvent(request);
        } catch (Exception e) {
            log.warn("publish workflow trace SSE failed eventId={} kind={} nodeId={} round={}: {}",
                    request.eventId(), request.kind(), request.nodeId(), request.roundIndex(), e.getMessage());
        }
    }

    public void applyWorkflowResult(WorkflowResultRequest request) {
        if (!resumeEvaluationService.acceptsWorkflowCallback(
                request.traceId(), request.workflowRunId(), request.conversationId(), request.revision())) {
            log.info("ignored stale workflow result trace={} run={} revision={}",
                    request.traceId(), request.workflowRunId(), request.revision());
            return;
        }
        boolean completedSuccessfully = resumeEvaluationService.applyWorkflowResult(request);
        if (completedSuccessfully && "SUCCESS".equals(request.status())) {
            agentMemoryService.recordWorkflowResult(request);
        }
    }

    private String mapEventKindToToolCall(String kind) {
        if ("generation".equals(kind)) {
            return "LLM_GENERATION";
        }
        if ("tool".equals(kind)) {
            return "LLM_TOOL_CALL";
        }
        if ("final".equals(kind)) {
            return "AGENT_FINAL";
        }
        if ("node".equals(kind)) {
            return "AGENT_EXECUTION";
        }
        return kind != null ? kind : "AGENT_EXECUTION";
    }

    private String buildPayloadJson(WorkflowTraceEventRequest request) throws Exception {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("eventId", request.eventId());
        payload.put("nodeId", request.nodeId());
        payload.put("agentName", request.agentName());
        payload.put("phase", request.phase());
        payload.put("attempt", request.attempt());
        payload.put("kind", request.kind());
        payload.put("roundIndex", request.roundIndex());
        payload.put("parentEventId", request.parentEventId());
        payload.put("status", request.status());
        payload.put("startedAt", request.startedAt());
        payload.put("endedAt", request.endedAt());
        payload.put("durationMs", request.durationMs());
        payload.put("modelName", request.modelName());
        payload.put("inputPreview", request.inputPreview());
        payload.put("outputPreview", request.outputPreview());
        payload.put("inputMessageCount", request.inputMessages() != null ? request.inputMessages().size() : 0);
        payload.put("hasOutputMessage", request.outputMessage() != null);
        List<Map<String, Object>> toolCalls = new ArrayList<>();
        if (request.toolCalls() != null) {
            for (WorkflowTraceEventRequest.WorkflowToolCallRecord tc : request.toolCalls()) {
                Map<String, Object> entry = new LinkedHashMap<>();
                entry.put("toolCallId", tc.toolCallId());
                entry.put("name", tc.name());
                entry.put("type", tc.category());
                entry.put("category", tc.category());
                entry.put("origin", tc.origin());
                entry.put("family", tc.family());
                entry.put("protocol", tc.protocol());
                entry.put("server", tc.server());
                entry.put("operation", tc.operation());
                entry.put("arguments", tc.arguments());
                entry.put("result", tc.result());
                entry.put("durationMs", tc.durationMs());
                entry.put("status", tc.status());
                entry.put("startedAt", tc.startedAt());
                entry.put("endedAt", tc.endedAt());
                entry.put("inputHash", tc.inputHash());
                entry.put("dedupedCount", tc.dedupedCount());
                entry.put("substeps", tc.substeps());
                entry.put("retrieval", tc.retrieval());
                toolCalls.add(entry);
            }
        }
        payload.put("toolCalls", toolCalls);
        payload.put("callKind", request.callKind());
        payload.put("callName", request.callName());
        payload.put("roundRole", request.roundRole());
        payload.put("parentRoundId", request.parentRoundId());
        payload.put("decisionText", request.decisionText());
        payload.put("hasToolCalls", request.hasToolCalls());
        payload.put("finalOutput", request.finalOutput());
        payload.put("langfuseTraceId", request.langfuseTraceId());
        payload.put("langfuseObservationId", request.langfuseObservationId());
        payload.put("tokenUsage", request.tokenUsage());
        payload.put("observationKind", request.observationKind());
        payload.put("toolOrigin", request.toolOrigin());
        payload.put("toolFamily", request.toolFamily());
        payload.put("substeps", request.substeps());
        payload.put("retrieval", request.retrieval());
        payload.put("workflowRunId", request.workflowRunId());
        payload.put("conversationId", request.conversationId());
        payload.put("revision", request.revision());
        return objectMapper.writeValueAsString(payload);
    }

    private void recordWorkflowMetrics(WorkflowTraceEventRequest request) {
        String kind = request.kind();
        String nodeId = request.nodeId();
        String agent = request.agentName();
        String status = request.status() != null ? request.status() : "SUCCESS";
        long duration = request.durationMs() != null ? request.durationMs() : 0L;

        if ("node".equals(kind)) {
            agentMetrics.recordWorkflowNode(request.traceId(), nodeId, agent, status, duration);
            return;
        }
        if ("generation".equals(kind)) {
            long tokens = totalTokens(request.tokenUsage());
            agentMetrics.recordWorkflowGeneration(
                    nodeId, agent, request.modelName(), request.roundRole(), status, duration, tokens);
            String modelName = request.modelName() != null ? request.modelName() : "";
            String callKind = request.callKind() != null ? request.callKind() : "";
            if (modelName.startsWith("deterministic-") || callKind.startsWith("system_")
                    || "system_plan".equals(request.observationKind())
                    || "system_fusion".equals(request.observationKind())) {
                String stage = StringUtils.hasText(callKind) ? callKind : modelName;
                agentMetrics.recordWorkflowHarness(
                        nodeId,
                        agent,
                        stage,
                        status,
                        duration,
                        byteLen(request.inputPreview()),
                        byteLen(request.outputPreview()));
            }
            return;
        }
        if ("tool".equals(kind)) {
            List<WorkflowTraceEventRequest.WorkflowToolCallRecord> calls =
                    request.toolCalls() != null ? request.toolCalls() : List.of();
            for (WorkflowTraceEventRequest.WorkflowToolCallRecord tc : calls) {
                String toolName = tc.name();
                String callKind = request.toolFamily() != null ? request.toolFamily()
                        : (request.callKind() != null ? request.callKind() : tc.family() != null ? tc.family() : tc.category());
                agentMetrics.recordWorkflowTool(
                        nodeId,
                        agent,
                        callKind,
                        toolName,
                        tc.status() != null ? tc.status() : status,
                        tc.durationMs() != null ? tc.durationMs() : duration,
                        byteLen(tc.arguments()),
                        byteLen(tc.result()));
                recordRagIfPresent(nodeId, toolName, tc.result());
            }
            return;
        }
        if ("final".equals(kind)) {
            agentMetrics.recordWorkflowNode(request.traceId(), nodeId, agent, status, duration);
        }
    }

    private void recordRagIfPresent(String nodeId, String toolName, String resultJson) {
        if (!Set.of("milvus_resume_search", "milvus_resume_batch_search", "milvus_jd_search", "mcp_resume_evidence_search").contains(toolName)) {
            return;
        }
        if (!StringUtils.hasText(resultJson)) {
            return;
        }
        try {
            Map<String, Object> data = objectMapper.readValue(resultJson, new TypeReference<>() {});
            int hitCount = intValue(data.get("hitCount"), inferHitCount(data));
            double topScore = doubleValue(data.get("topScore"), inferTopScore(data));
            boolean fallbackUsed = boolValue(data.get("fallbackUsed"));
            String fallbackReason = stringValue(data.get("fallbackReason"), "none");
            agentMetrics.recordWorkflowRag(nodeId, toolName, hitCount, topScore, fallbackUsed, fallbackReason);
        } catch (Exception e) {
            log.debug("recordRagIfPresent parse failed tool={}: {}", toolName, e.getMessage());
        }
    }

    private static long totalTokens(Map<String, Object> usage) {
        if (usage == null || !(usage.get("total_tokens") instanceof Number n)) {
            return 0L;
        }
        return n.longValue();
    }

    private static int byteLen(String value) {
        return value == null ? 0 : value.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
    }

    private static int intValue(Object value, int defaultValue) {
        if (value instanceof Number n) {
            return n.intValue();
        }
        return defaultValue;
    }

    private static double doubleValue(Object value, double defaultValue) {
        if (value instanceof Number n) {
            return n.doubleValue();
        }
        return defaultValue;
    }

    private static boolean boolValue(Object value) {
        if (value instanceof Boolean b) {
            return b;
        }
        return false;
    }

    private static String stringValue(Object value, String defaultValue) {
        return value != null ? String.valueOf(value) : defaultValue;
    }

    private static int inferHitCount(Map<String, Object> data) {
        Object items = data.get("items");
        if (items instanceof List<?> list) {
            return list.size();
        }
        Object chunks = data.get("chunks");
        if (chunks instanceof List<?> list) {
            return list.size();
        }
        return 0;
    }

    private static double inferTopScore(Map<String, Object> data) {
        Object items = data.get("items");
        if (items instanceof List<?> list && !list.isEmpty() && list.get(0) instanceof Map<?, ?> first) {
            Object score = first.get("score");
            if (score instanceof Number n) {
                return n.doubleValue();
            }
        }
        return 0D;
    }

    private LocalDateTime parseWorkflowTime(String value) {
        if (!StringUtils.hasText(value)) {
            return null;
        }
        try {
            return OffsetDateTime.parse(value).toLocalDateTime();
        } catch (DateTimeParseException ignored) {
            try {
                return LocalDateTime.parse(value);
            } catch (DateTimeParseException e) {
                return null;
            }
        }
    }

    private String trim(String value, int max) {
        if (value == null || value.length() <= max) {
            return value;
        }
        return value.substring(0, max) + "...";
    }

    private String shortLegacySpanId(String eventId) {
        if (!StringUtils.hasText(eventId)) {
            return null;
        }
        return "span-" + Integer.toUnsignedString(eventId.hashCode());
    }
}
