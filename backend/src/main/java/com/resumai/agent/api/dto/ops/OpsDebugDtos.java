package com.resumai.agent.api.dto.ops;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

public final class OpsDebugDtos {

    private OpsDebugDtos() {
    }

    public enum EventOutcome {
        FAILED, SUCCESS, RUNNING, INFO
    }

    public record RunDebugSummary(
            String runId,
            String conversationId,
            String userId,
            String traceId,
            String sourceTaskTraceId,
            Integer revisionNo,
            String runType,
            String status,
            String currentAgent,
            String currentTool,
            String currentPhase,
            String policyId,
            String errorCode,
            String errorMessage,
            Object skillVersions,
            Object promptVersions,
            Object metrics,
            LocalDateTime createdAt,
            LocalDateTime startedAt,
            LocalDateTime finishedAt,
            LocalDateTime updatedAt,
            Long queueWaitMs,
            Long runtimeMs,
            Long durationMs
    ) {
    }

    public record CorrelationView(
            String runId,
            String conversationId,
            String traceId,
            String sourceTaskTraceId,
            Integer revisionNo,
            String policyId,
            List<Map<String, Object>> siblingRevisions,
            List<Map<String, Object>> retryChain
    ) {
    }

    public record PlanDebugView(
            List<String> plan,
            List<List<String>> parallelGroups,
            String reason,
            String requiredTerminalAgent,
            String policyId,
            Map<String, Object> selectedBecause,
            Map<String, Object> skippedBecause,
            List<Object> artifactEdges,
            List<String> goalArtifacts,
            Object budgetPlan,
            boolean present
    ) {
        public static PlanDebugView empty() {
            return new PlanDebugView(List.of(), List.of(), null, null, null,
                    Map.of(), Map.of(), List.of(), List.of(), Map.of(), false);
        }
    }

    public record BudgetDebugView(
            Object planned,
            Object actualMetrics,
            Integer llmCalls,
            Integer toolCalls,
            Integer promptTokens,
            Integer completionTokens,
            Object cost
    ) {
    }

    public record ArtifactDebugView(
            Map<String, Object> artifacts,
            List<String> presentKeys,
            List<String> requiredKeys,
            List<String> missingKeys,
            List<Object> edges
    ) {
    }

    public record TimelineEventView(
            Integer seq,
            String eventType,
            String agentId,
            String toolName,
            EventOutcome outcome,
            Object payload,
            LocalDateTime createTime
    ) {
    }

    public record ErrorDiagnosticView(
            Integer seq,
            String eventType,
            String agentId,
            String toolName,
            String errorCode,
            String message,
            Object payload,
            LocalDateTime createTime,
            boolean rootCause
    ) {
    }

    public record McpInvocationView(
            String runId,
            String traceId,
            Integer seq,
            String toolCallId,
            String server,
            String tool,
            String agent,
            String outcome,
            Long durationMs,
            Integer retryCount,
            Boolean cacheHit,
            Object arguments,
            Object resultPreview,
            String error,
            LocalDateTime createTime
    ) {
    }

    public record SkillUsageView(
            String runId,
            String skillId,
            String agentId,
            String eventType,
            String triggerReason,
            String skillVersion,
            String skillHash,
            String runHash,
            String manifestHash,
            Boolean hashDrift,
            List<String> requiredMcp,
            Object payload,
            LocalDateTime createTime
    ) {
    }

    public record MemoryUsageView(
            Long id,
            String runId,
            String memoryId,
            String consumerAgent,
            Integer rankNo,
            Double vectorScore,
            Double lexicalScore,
            Double recencyScore,
            Double finalScore,
            String decision,
            String ignoredReason,
            LocalDateTime createTime,
            String type,
            String ownerScope,
            String source,
            String contentPreview
    ) {
    }

    public record ObservabilityView(
            Map<String, Object> langfuse
    ) {
    }

    public record RunDebugDetailResponse(
            RunDebugSummary run,
            CorrelationView correlation,
            PlanDebugView plan,
            BudgetDebugView budget,
            ArtifactDebugView artifacts,
            List<TimelineEventView> timeline,
            List<ErrorDiagnosticView> errors,
            List<McpInvocationView> mcpCalls,
            List<SkillUsageView> skills,
            List<MemoryUsageView> memory,
            ObservabilityView observability,
            boolean truncated,
            Integer nextSeq
    ) {
    }

    public record McpInventoryServer(
            String name,
            String status,
            String transport,
            String description,
            Long latencyMs,
            Boolean circuitOpen,
            Boolean optional,
            List<String> tools,
            String error
    ) {
    }

    public record McpInventory(
            String source,
            boolean runtimeReachable,
            boolean probed,
            Object lastProbeAt,
            Object availableTools,
            Object toolCount,
            Object configPath,
            List<McpInventoryServer> servers,
            String runtimeError
    ) {
    }

    public record McpInvocationPage(
            int count,
            List<McpInvocationView> items
    ) {
    }

    public record McpOpsResponse(
            McpInventory inventory,
            McpInvocationPage invocations,
            List<String> statusEnum,
            String note
    ) {
    }

    public record SkillManifestItem(
            String skillId,
            String name,
            String version,
            String hash,
            String status,
            String description,
            Boolean deprecated,
            Boolean adminOnly,
            List<String> requiredTools,
            List<String> allowedTools
    ) {
    }

    public record SkillAggUsage(
            String skillId,
            long selected,
            long applied,
            long failed,
            String lastRunId,
            LocalDateTime lastAt,
            String lastHash,
            String lastVersion
    ) {
    }

    public record SkillOpsResponse(
            String source,
            boolean runtimeReachable,
            Object root,
            int count,
            int activeCount,
            int deprecatedCount,
            List<String> advertisedTools,
            List<SkillManifestItem> skills,
            List<SkillUsageView> selectedApplied,
            List<SkillAggUsage> usageBySkill,
            String runtimeError,
            String note
    ) {
    }

    public record MemoryOpsResponse(
            int count,
            int skipped,
            Map<String, Long> byType,
            Map<String, Long> byScope,
            Map<String, Long> bySource,
            List<Map<String, Object>> entries,
            List<MemoryUsageView> usage,
            Map<String, Object> defaults,
            Map<String, Object> fileStore
    ) {
    }
}
