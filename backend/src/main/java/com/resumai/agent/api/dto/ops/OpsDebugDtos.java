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
            String lifecycleStage,
            String outcome,
            Long durationMs,
            Integer retryCount,
            Boolean cacheHit,
            Object arguments,
            Object resultPreview,
            String error,
            String occurredAt,
            String startedAt,
            String endedAt,
            LocalDateTime createTime
    ) {
    }

    public record SkillUsageView(
            String runId,
            String skillId,
            String agentId,
            String eventType,
            String lifecycleStage,
            String triggerReason,
            String skillVersion,
            String skillHash,
            String runHash,
            String manifestHash,
            Boolean hashDrift,
            List<String> requiredMcp,
            Object payload,
            String occurredAt,
            String startedAt,
            String endedAt,
            LocalDateTime createTime
    ) {
    }

    public record MemoryTtlView(
            String mode,
            String state,
            String expiresAt,
            Long effectiveTtlSeconds,
            Long remainingTtlSeconds,
            Double remainingPercent,
            Long typeDefaultDays,
            boolean overrideDetected,
            boolean renewOnUse
    ) {
    }

    public record MemoryUsageView(
            Long id,
            String runId,
            String memoryId,
            String consumerAgent,
            String consumerVersion,
            String producerVersion,
            Integer rankNo,
            Double vectorScore,
            Double lexicalScore,
            Double recencyScore,
            Double finalScore,
            String decision,
            String ignoredReason,
            String occurredAt,
            LocalDateTime createTime,
            String type,
            String ownerScope,
            String source,
            String contentPreview,
            String memoryCreatedAt,
            String memoryUpdatedAt,
            Long ageAtUseSeconds,
            MemoryTtlView ttl
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
            long catalog,
            long selected,
            long loaded,
            long applied,
            long skipped,
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

    /**
     * Stage timings are nullable on purpose. Historical events did not split
     * embedding from retrieval, so {@code embeddingRetrievalMs} preserves that
     * combined measurement without pretending that either individual stage was
     * measured.
     */
    public record RagStageTimingView(
            Double queryRewriteMs,
            Double embeddingMs,
            Double retrievalMs,
            Double embeddingRetrievalMs,
            Double fusionMs,
            Double rerankMs,
            Double totalMs
    ) {
    }

    public record RagChunkView(
            String chunkId,
            String documentId,
            String title,
            String source,
            String uri,
            Double score,
            String scoreType,
            Integer rank,
            String preview,
            Object provenance
    ) {
    }

    /**
     * Precision/recall are populated only when a labelled relevance set is
     * explicitly attached to the event. Groundedness is populated only when a
     * named judge completed successfully. Retrieval scores are otherwise
     * exposed as ranking proxies, never as ground-truth quality metrics.
     */
    public record RagQualityView(
            boolean groundTruthAvailable,
            String judgeSource,
            Double precisionAtK,
            Double recallAtK,
            Double groundedness,
            String relevanceScoreSemantics,
            String note
    ) {
    }

    public record RagRetrievalView(
            String runId,
            String traceId,
            Integer seq,
            String toolCallId,
            String toolName,
            String agentId,
            String query,
            String querySummary,
            List<String> queriesUsed,
            String outcome,
            String occurredAt,
            String startedAt,
            String endedAt,
            String retrievedAt,
            Long durationMs,
            String strategy,
            String fusionStrategy,
            String indexName,
            String source,
            Integer requestedK,
            Integer returnedK,
            Integer uniqueDocuments,
            Integer candidateCount,
            Integer lexicalHits,
            Integer vectorHits,
            Integer filteredCount,
            Integer droppedCount,
            Integer deduplicatedCount,
            Boolean zeroHit,
            Double topScore,
            Double meanScore,
            Double minScore,
            Double scoreSpread,
            Integer scoreSampleSize,
            Boolean rerankApplied,
            Double rerankBeforeTopScore,
            Double rerankAfterTopScore,
            Double rerankLift,
            Boolean cacheHit,
            Boolean fallback,
            String fallbackStage,
            List<String> fallbackChain,
            Boolean degraded,
            String degradationReason,
            String error,
            RagStageTimingView stages,
            List<RagChunkView> chunks,
            RagQualityView quality,
            boolean telemetryComplete
    ) {
    }

    public record RagStageAggregateView(
            String stage,
            int samples,
            Double averageMs,
            Double p90Ms,
            Double averageShare
    ) {
    }

    public record RagOpsSummary(
            int volume,
            int terminalCount,
            int successCount,
            int zeroHitCount,
            int zeroHitEligibleCount,
            int errorCount,
            int degradedCount,
            int cacheHitCount,
            Double successRate,
            Double zeroHitRate,
            Double p50LatencyMs,
            Double p90LatencyMs,
            Double averageTopScoreProxy,
            Double averageReturnedK,
            Double topKFillRateProxy,
            Double averageRerankLift,
            int rerankLiftSamples,
            String bottleneckStage,
            Double bottleneckAverageMs,
            List<RagStageAggregateView> stageBreakdown,
            int completeTelemetryCount
    ) {
    }

    public record RagOpsResponse(
            String schemaVersion,
            LocalDateTime generatedAt,
            int count,
            RagOpsSummary summary,
            List<RagRetrievalView> items,
            Map<String, Object> metricSemantics,
            List<String> warnings
    ) {
    }
}
