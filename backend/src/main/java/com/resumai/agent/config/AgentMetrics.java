package com.resumai.agent.config;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.DistributionSummary;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.IntSupplier;
import org.springframework.stereotype.Component;

/**
 * ResumAI 多 Agent 编排可观测性指标（6 大维度、70+ 指标）。
 */
@Component
public class AgentMetrics {

    private final MeterRegistry registry;
    private final AtomicInteger concurrentActiveAgents = new AtomicInteger(0);
    private final AtomicLong totalLlmOutputTokens = new AtomicLong(0);
    private final AtomicLong totalLlmInputTokens = new AtomicLong(0);
    private final AtomicLong funnelCompleted = new AtomicLong(0);
    private final AtomicLong funnelStarted = new AtomicLong(0);

    public AgentMetrics(MeterRegistry registry) {
        this.registry = registry;
        Gauge.builder("resumai.agent.concurrent.active", concurrentActiveAgents, AtomicInteger::get)
                .description("当前并发执行的 Agent 任务数")
                .register(registry);
        Gauge.builder("resumai.llm.token_efficiency", this, AgentMetrics::tokenEfficiencyRatio)
                .description("LLM Token 产出效率（output/input）")
                .register(registry);
        Gauge.builder("resumai.funnel.conversion_rate", this, AgentMetrics::funnelConversionRate)
                .description("评估漏斗转化率（completed/started）")
                .register(registry);
        initializeMetricCatalog();
    }

    /**
     * 预注册全部指标名称，确保 /actuator/prometheus 在未触发业务前也能暴露完整 catalog。
     */
    private void initializeMetricCatalog() {
        String[] agents = {"OrchestratorAgent", "ResumeParserAgent", "TechAgent"};
        String[] tools = {"milvus_index", "milvus_retrieve", "neo4j_populate"};
        String[] models = {"deepseek-chat"};
        String[] stages = {"ResumeParserAgent", "TechAgent"};

        for (String agent : agents) {
            Timer.builder("resumai.agent.span.duration").tag("agent", agent).tag("status", "SUCCESS")
                    .tag("parent_agent", "OrchestratorAgent").register(registry);
            Counter.builder("resumai.agent.span.count").tag("agent", agent).tag("status", "SUCCESS").register(registry);
            DistributionSummary.builder("resumai.agent.iteration.count").tag("agent", agent).register(registry);
            Counter.builder("resumai.agent.error").tag("agent", agent).tag("error_type", "none").register(registry);
            Counter.builder("resumai.agent.skill.invoked").tag("agent", agent).tag("skill_name", "init")
                    .tag("found", "false").register(registry);
        }
        Counter.builder("resumai.agent.delegation").tag("from_agent", "OrchestratorAgent")
                .tag("to_agent", "ResumeParserAgent").register(registry);
        Timer.builder("resumai.agent.delegation.latency").tag("from_agent", "OrchestratorAgent")
                .tag("to_agent", "ResumeParserAgent").register(registry);
        Counter.builder("resumai.agent.routing.decision").tag("decision", "DEFAULT")
                .tag("job_category", "TECH").register(registry);

        for (String tool : tools) {
            Timer.builder("resumai.tool.call.duration").tag("tool_name", tool).tag("agent", "init")
                    .tag("status", "SUCCESS").register(registry);
            Counter.builder("resumai.tool.call.count").tag("tool_name", tool).tag("agent", "init")
                    .tag("status", "SUCCESS").register(registry);
            Counter.builder("resumai.tool.call.error").tag("tool_name", tool).tag("error_type", "none").register(registry);
            DistributionSummary.builder("resumai.tool.call.input_size").tag("tool_name", tool).register(registry);
            DistributionSummary.builder("resumai.tool.call.output_size").tag("tool_name", tool).register(registry);
            Counter.builder("resumai.tool.wasted_calls").tag("tool_name", tool).tag("agent", "init").register(registry);
        }
        DistributionSummary.builder("resumai.tool.pdf.pages_extracted").register(registry);
        DistributionSummary.builder("resumai.tool.pdf.text_length").register(registry);
        DistributionSummary.builder("resumai.tool.neo4j.nodes_written").tag("operation", "populateGraph").register(registry);
        DistributionSummary.builder("resumai.tool.neo4j.relationships_written").tag("operation", "populateGraph").register(registry);
        DistributionSummary.builder("resumai.tool.milvus.chunks_indexed").register(registry);
        DistributionSummary.builder("resumai.tool.milvus.chunks_retrieved").register(registry);
        DistributionSummary.builder("resumai.tool.milvus.similarity_score").register(registry);

        for (String model : models) {
            DistributionSummary.builder("resumai.llm.tokens.input").tag("model", model).tag("agent", "init")
                    .tag("purpose", "evaluation").register(registry);
            DistributionSummary.builder("resumai.llm.tokens.output").tag("model", model).tag("agent", "init")
                    .tag("purpose", "evaluation").register(registry);
            Counter.builder("resumai.llm.tokens.total").tag("model", model).register(registry);
            DistributionSummary.builder("resumai.llm.cost.per_call").tag("model", model).tag("agent", "init").register(registry);
            DistributionSummary.builder("resumai.llm.context_utilization").tag("model", model).register(registry);
            Timer.builder("resumai.llm.duration").tag("model", model).tag("agent", "init")
                    .tag("purpose", "evaluation").register(registry);
            Counter.builder("resumai.llm.retry.count").tag("model", model).tag("reason", "none").register(registry);
            Counter.builder("resumai.llm.error").tag("model", model).tag("error_type", "none").register(registry);
        }
        DistributionSummary.builder("resumai.llm.cost.per_task").register(registry);

        Counter.builder("resumai.funnel.upload.count").tag("file_type", "pdf").tag("job_category", "TECH").register(registry);
        DistributionSummary.builder("resumai.funnel.upload.size_bytes").tag("file_type", "pdf").register(registry);
        Counter.builder("resumai.funnel.parse.success").tag("file_type", "pdf").register(registry);
        Counter.builder("resumai.funnel.parse.failure").tag("file_type", "pdf").tag("reason", "none").register(registry);
        Counter.builder("resumai.funnel.evaluation.started").tag("job_category", "TECH")
                .tag("execution_mode", "SERIAL").register(registry);
        Counter.builder("resumai.funnel.evaluation.completed").tag("job_category", "TECH")
                .tag("status", "SUCCESS").tag("recommendation", "RECOMMEND").register(registry);
        Counter.builder("resumai.funnel.evaluation.dropped").tag("job_category", "TECH").tag("reason", "none").register(registry);
        Timer.builder("resumai.funnel.time_to_screen").tag("job_category", "TECH").register(registry);
        for (String stage : stages) {
            Timer.builder("resumai.funnel.time_in_stage").tag("stage_name", stage).register(registry);
        }
        DistributionSummary.builder("resumai.funnel.score.distribution").tag("job_category", "TECH").register(registry);
        Counter.builder("resumai.funnel.recommendation.count").tag("recommendation", "RECOMMEND").register(registry);
        Counter.builder("resumai.funnel.feedback.submitted").tag("rating", "5").register(registry);
        Counter.builder("resumai.funnel.feedback.agreement").tag("agrees_with_ai", "true").register(registry);
        Timer.builder("resumai.funnel.feedback.latency").register(registry);
        Counter.builder("resumai.funnel.daily_volume").register(registry);

        DistributionSummary.builder("resumai.rag.faithfulness").register(registry);
        DistributionSummary.builder("resumai.rag.answer_relevancy").register(registry);
        DistributionSummary.builder("resumai.rag.context_precision").register(registry);
        DistributionSummary.builder("resumai.rag.overall_quality").register(registry);
        Counter.builder("resumai.rag.retrieval.empty_results").register(registry);
        Counter.builder("resumai.rag.retrieval.below_threshold").register(registry);
    }

    // ── Dimension 1: Agent Execution ─────────────────────────────────────────

    public void recordAgentSpan(String agent, String status, String parentAgent, long durationMs) {
        Timer.builder("resumai.agent.span.duration")
                .description("Agent Span 执行耗时")
                .tag("agent", safeTag(agent))
                .tag("status", safeTag(status))
                .tag("parent_agent", safeTag(parentAgent))
                .register(registry)
                .record(Duration.ofMillis(durationMs));
        Counter.builder("resumai.agent.span.count")
                .description("Agent Span 执行次数")
                .tag("agent", safeTag(agent))
                .tag("status", safeTag(status))
                .register(registry)
                .increment();
    }

    public void recordAgentDelegation(String fromAgent, String toAgent) {
        Counter.builder("resumai.agent.delegation")
                .description("Agent 委派次数")
                .tag("from_agent", safeTag(fromAgent))
                .tag("to_agent", safeTag(toAgent))
                .register(registry)
                .increment();
    }

    public void recordAgentDelegationLatency(String fromAgent, String toAgent, long durationMs) {
        Timer.builder("resumai.agent.delegation.latency")
                .description("Agent 委派延迟")
                .tag("from_agent", safeTag(fromAgent))
                .tag("to_agent", safeTag(toAgent))
                .register(registry)
                .record(Duration.ofMillis(durationMs));
    }

    public void recordAgentIterationCount(String agent, int iterations) {
        DistributionSummary.builder("resumai.agent.iteration.count")
                .description("Agent 迭代次数分布")
                .tag("agent", safeTag(agent))
                .register(registry)
                .record(iterations);
    }

    public void agentTaskStarted() {
        concurrentActiveAgents.incrementAndGet();
    }

    public void agentTaskFinished() {
        concurrentActiveAgents.decrementAndGet();
    }

    public void recordAgentError(String agent, String errorType) {
        Counter.builder("resumai.agent.error")
                .description("Agent 错误次数")
                .tag("agent", safeTag(agent))
                .tag("error_type", safeTag(errorType))
                .register(registry)
                .increment();
    }

    public void recordSkillInvoked(String agent, String skillName, boolean found) {
        Counter.builder("resumai.agent.skill.invoked")
                .description("Skill 调用次数")
                .tag("agent", safeTag(agent))
                .tag("skill_name", safeTag(skillName))
                .tag("found", String.valueOf(found))
                .register(registry)
                .increment();
    }

    public void recordRoutingDecision(String decision, String jobCategory) {
        Counter.builder("resumai.agent.routing.decision")
                .description("编排路由决策次数")
                .tag("decision", safeTag(decision))
                .tag("job_category", safeTag(jobCategory))
                .register(registry)
                .increment();
    }

    // ── Dimension 2: Tool Calls ──────────────────────────────────────────────

    public void recordToolCall(String toolName, String agent, String status, long durationMs) {
        Timer.builder("resumai.tool.call.duration")
                .description("工具调用耗时")
                .tag("tool_name", safeTag(toolName))
                .tag("agent", safeTag(agent))
                .tag("status", safeTag(status))
                .register(registry)
                .record(Duration.ofMillis(durationMs));
        Counter.builder("resumai.tool.call.count")
                .description("工具调用次数")
                .tag("tool_name", safeTag(toolName))
                .tag("agent", safeTag(agent))
                .tag("status", safeTag(status))
                .register(registry)
                .increment();
    }

    public void recordToolCallError(String toolName, String errorType) {
        Counter.builder("resumai.tool.call.error")
                .description("工具调用错误次数")
                .tag("tool_name", safeTag(toolName))
                .tag("error_type", safeTag(errorType))
                .register(registry)
                .increment();
    }

    public void recordToolInputSize(String toolName, int bytes) {
        DistributionSummary.builder("resumai.tool.call.input_size")
                .description("工具输入大小分布")
                .tag("tool_name", safeTag(toolName))
                .register(registry)
                .record(bytes);
    }

    public void recordToolOutputSize(String toolName, int bytes) {
        DistributionSummary.builder("resumai.tool.call.output_size")
                .description("工具输出大小分布")
                .tag("tool_name", safeTag(toolName))
                .register(registry)
                .record(bytes);
    }

    public void recordWastedToolCall(String toolName, String agent) {
        Counter.builder("resumai.tool.wasted_calls")
                .description("无效工具调用次数")
                .tag("tool_name", safeTag(toolName))
                .tag("agent", safeTag(agent))
                .register(registry)
                .increment();
    }

    public void recordPdfPagesExtracted(int pages) {
        DistributionSummary.builder("resumai.tool.pdf.pages_extracted")
                .description("PDF 抽取页数分布")
                .register(registry)
                .record(pages);
    }

    public void recordPdfTextLength(int length) {
        DistributionSummary.builder("resumai.tool.pdf.text_length")
                .description("PDF 抽取文本长度分布")
                .register(registry)
                .record(length);
    }

    public void recordNeo4jNodesWritten(String operation, int count) {
        DistributionSummary.builder("resumai.tool.neo4j.nodes_written")
                .description("Neo4j 写入节点数")
                .tag("operation", safeTag(operation))
                .register(registry)
                .record(count);
    }

    public void recordNeo4jRelationshipsWritten(String operation, int count) {
        DistributionSummary.builder("resumai.tool.neo4j.relationships_written")
                .description("Neo4j 写入关系数")
                .tag("operation", safeTag(operation))
                .register(registry)
                .record(count);
    }

    public void recordMilvusChunksIndexed(int count) {
        DistributionSummary.builder("resumai.tool.milvus.chunks_indexed")
                .description("Milvus 索引 chunk 数")
                .register(registry)
                .record(count);
    }

    public void recordMilvusChunksRetrieved(int count) {
        DistributionSummary.builder("resumai.tool.milvus.chunks_retrieved")
                .description("Milvus 检索 chunk 数")
                .register(registry)
                .record(count);
    }

    public void recordMilvusSimilarityScore(double score) {
        DistributionSummary.builder("resumai.tool.milvus.similarity_score")
                .description("Milvus 相似度分数分布")
                .register(registry)
                .record(score);
    }

    // ── Dimension 3: LLM Economics ───────────────────────────────────────────

    public void recordLlmTokens(String model, String agent, String purpose, int inputTokens, int outputTokens) {
        DistributionSummary.builder("resumai.llm.tokens.input")
                .description("LLM 输入 Token 分布")
                .tag("model", safeTag(model))
                .tag("agent", safeTag(agent))
                .tag("purpose", safeTag(purpose))
                .register(registry)
                .record(inputTokens);
        DistributionSummary.builder("resumai.llm.tokens.output")
                .description("LLM 输出 Token 分布")
                .tag("model", safeTag(model))
                .tag("agent", safeTag(agent))
                .tag("purpose", safeTag(purpose))
                .register(registry)
                .record(outputTokens);
        Counter.builder("resumai.llm.tokens.total")
                .description("LLM Token 总量")
                .tag("model", safeTag(model))
                .register(registry)
                .increment(inputTokens + outputTokens);
        totalLlmInputTokens.addAndGet(inputTokens);
        totalLlmOutputTokens.addAndGet(outputTokens);
    }

    public void recordLlmCostPerCall(String model, String agent, double costUsd) {
        DistributionSummary.builder("resumai.llm.cost.per_call")
                .description("单次 LLM 调用成本（USD）")
                .tag("model", safeTag(model))
                .tag("agent", safeTag(agent))
                .register(registry)
                .record(costUsd);
    }

    public void recordLlmCostPerTask(double costUsd) {
        DistributionSummary.builder("resumai.llm.cost.per_task")
                .description("单任务 LLM 成本（USD）")
                .register(registry)
                .record(costUsd);
    }

    public void recordLlmContextUtilization(String model, double utilization) {
        DistributionSummary.builder("resumai.llm.context_utilization")
                .description("LLM 上下文窗口利用率")
                .tag("model", safeTag(model))
                .register(registry)
                .record(utilization);
    }

    public void recordLlmDuration(String model, String agent, String purpose, long durationMs) {
        Timer.builder("resumai.llm.duration")
                .description("LLM 调用耗时")
                .tag("model", safeTag(model))
                .tag("agent", safeTag(agent))
                .tag("purpose", safeTag(purpose))
                .register(registry)
                .record(Duration.ofMillis(durationMs));
    }

    public void recordLlmRetry(String model, String reason) {
        Counter.builder("resumai.llm.retry.count")
                .description("LLM 重试次数")
                .tag("model", safeTag(model))
                .tag("reason", safeTag(reason))
                .register(registry)
                .increment();
    }

    public void recordLlmError(String model, String errorType) {
        Counter.builder("resumai.llm.error")
                .description("LLM 错误次数")
                .tag("model", safeTag(model))
                .tag("error_type", safeTag(errorType))
                .register(registry)
                .increment();
    }

    // ── Dimension 4: Business Funnel ─────────────────────────────────────────

    public void recordFunnelUpload(String fileType, String jobCategory) {
        Counter.builder("resumai.funnel.upload.count")
                .description("简历上传次数")
                .tag("file_type", safeTag(fileType))
                .tag("job_category", safeTag(jobCategory))
                .register(registry)
                .increment();
    }

    public void recordFunnelUploadSize(String fileType, long sizeBytes) {
        DistributionSummary.builder("resumai.funnel.upload.size_bytes")
                .description("简历上传大小分布")
                .tag("file_type", safeTag(fileType))
                .register(registry)
                .record(sizeBytes);
    }

    public void recordFunnelParseSuccess(String fileType) {
        Counter.builder("resumai.funnel.parse.success")
                .description("简历解析成功次数")
                .tag("file_type", safeTag(fileType))
                .register(registry)
                .increment();
    }

    public void recordFunnelParseFailure(String fileType, String reason) {
        Counter.builder("resumai.funnel.parse.failure")
                .description("简历解析失败次数")
                .tag("file_type", safeTag(fileType))
                .tag("reason", safeTag(reason))
                .register(registry)
                .increment();
    }

    public void recordFunnelEvaluationStarted(String jobCategory, String executionMode) {
        funnelStarted.incrementAndGet();
        Counter.builder("resumai.funnel.evaluation.started")
                .description("评估任务启动次数")
                .tag("job_category", safeTag(jobCategory))
                .tag("execution_mode", safeTag(executionMode))
                .register(registry)
                .increment();
        Counter.builder("resumai.funnel.daily_volume")
                .description("每日评估量")
                .register(registry)
                .increment();
    }

    public void recordFunnelEvaluationCompleted(String jobCategory, String status, String recommendation) {
        if ("SUCCESS".equals(status)) {
            funnelCompleted.incrementAndGet();
        }
        Counter.builder("resumai.funnel.evaluation.completed")
                .description("评估任务完成次数")
                .tag("job_category", safeTag(jobCategory))
                .tag("status", safeTag(status))
                .tag("recommendation", safeTag(recommendation))
                .register(registry)
                .increment();
    }

    public void recordFunnelEvaluationDropped(String jobCategory, String reason) {
        Counter.builder("resumai.funnel.evaluation.dropped")
                .description("评估任务丢弃次数")
                .tag("job_category", safeTag(jobCategory))
                .tag("reason", safeTag(reason))
                .register(registry)
                .increment();
    }

    public void recordFunnelTimeToScreen(String jobCategory, long durationMs) {
        Timer.builder("resumai.funnel.time_to_screen")
                .description("上传到初筛完成耗时")
                .tag("job_category", safeTag(jobCategory))
                .register(registry)
                .record(Duration.ofMillis(durationMs));
    }

    public void recordFunnelTimeInStage(String stageName, long durationMs) {
        Timer.builder("resumai.funnel.time_in_stage")
                .description("漏斗阶段停留耗时")
                .tag("stage_name", safeTag(stageName))
                .register(registry)
                .record(Duration.ofMillis(durationMs));
    }

    public void recordFunnelScoreDistribution(String jobCategory, int score) {
        DistributionSummary.builder("resumai.funnel.score.distribution")
                .description("评估分数分布")
                .tag("job_category", safeTag(jobCategory))
                .register(registry)
                .record(score);
    }

    public void recordFunnelRecommendation(String recommendation) {
        Counter.builder("resumai.funnel.recommendation.count")
                .description("推荐结论分布")
                .tag("recommendation", safeTag(recommendation))
                .register(registry)
                .increment();
    }

    public void recordFunnelFeedbackSubmitted(int rating) {
        Counter.builder("resumai.funnel.feedback.submitted")
                .description("HR 反馈提交次数")
                .tag("rating", String.valueOf(rating))
                .register(registry)
                .increment();
    }

    public void recordFunnelFeedbackAgreement(boolean agreesWithAi) {
        Counter.builder("resumai.funnel.feedback.agreement")
                .description("HR 与 AI 结论一致性")
                .tag("agrees_with_ai", String.valueOf(agreesWithAi))
                .register(registry)
                .increment();
    }

    public void recordFunnelFeedbackLatency(long durationMs) {
        Timer.builder("resumai.funnel.feedback.latency")
                .description("HR 反馈处理耗时")
                .register(registry)
                .record(Duration.ofMillis(durationMs));
    }

    // ── Dimension 5: RAG Quality ─────────────────────────────────────────────

    public void recordRagFaithfulness(double score) {
        DistributionSummary.builder("resumai.rag.faithfulness")
                .description("RAG 忠实度分数")
                .register(registry)
                .record(score);
    }

    public void recordRagAnswerRelevancy(double score) {
        DistributionSummary.builder("resumai.rag.answer_relevancy")
                .description("RAG 答案相关度")
                .register(registry)
                .record(score);
    }

    public void recordRagContextPrecision(double score) {
        DistributionSummary.builder("resumai.rag.context_precision")
                .description("RAG 上下文精确度")
                .register(registry)
                .record(score);
    }

    public void recordRagOverallQuality(double score) {
        DistributionSummary.builder("resumai.rag.overall_quality")
                .description("RAG 综合质量分数")
                .register(registry)
                .record(score);
    }

    public void recordRagRetrievalEmptyResults() {
        Counter.builder("resumai.rag.retrieval.empty_results")
                .description("RAG 检索空结果次数")
                .register(registry)
                .increment();
    }

    public void recordRagRetrievalBelowThreshold() {
        Counter.builder("resumai.rag.retrieval.below_threshold")
                .description("RAG 检索低于阈值次数")
                .register(registry)
                .increment();
    }

    // ── Dimension 6: System Health ───────────────────────────────────────────

    public void registerNeo4jConnectionPoolGauge(IntSupplier poolSizeSupplier) {
        Gauge.builder("resumai.system.neo4j.connection_pool", poolSizeSupplier, IntSupplier::getAsInt)
                .description("Neo4j 连接池大小")
                .register(registry);
    }

    public void registerMilvusConnectionAliveGauge(IntSupplier aliveSupplier) {
        Gauge.builder("resumai.system.milvus.connection_alive", aliveSupplier, IntSupplier::getAsInt)
                .description("Milvus 连接存活状态")
                .register(registry);
    }

    public void registerExecutorActiveThreadsGauge(IntSupplier activeThreadsSupplier) {
        Gauge.builder("resumai.system.executor.active_threads", activeThreadsSupplier, IntSupplier::getAsInt)
                .description("线程池活跃线程数")
                .register(registry);
    }

    public void registerExecutorQueueSizeGauge(IntSupplier queueSizeSupplier) {
        Gauge.builder("resumai.system.executor.queue_size", queueSizeSupplier, IntSupplier::getAsInt)
                .description("线程池队列长度")
                .register(registry);
    }

    public void registerSseActiveSubscribersGauge(IntSupplier subscribersSupplier) {
        Gauge.builder("resumai.system.sse.active_subscribers", subscribersSupplier, IntSupplier::getAsInt)
                .description("SSE 活跃订阅数")
                .register(registry);
    }

    public void registerTaskCacheSizeGauge(IntSupplier cacheSizeSupplier) {
        Gauge.builder("resumai.system.memory.task_cache_size", cacheSizeSupplier, IntSupplier::getAsInt)
                .description("内存任务缓存大小")
                .register(registry);
    }

    private double tokenEfficiencyRatio() {
        long input = totalLlmInputTokens.get();
        if (input == 0) {
            return 0D;
        }
        return (double) totalLlmOutputTokens.get() / input;
    }

    private double funnelConversionRate() {
        long started = funnelStarted.get();
        if (started == 0) {
            return 0D;
        }
        return (double) funnelCompleted.get() / started;
    }

    private static String safeTag(String value) {
        return value == null || value.isBlank() ? "unknown" : value;
    }
}
