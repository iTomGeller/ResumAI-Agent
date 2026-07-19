package com.resumai.agent.ai;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.ai.agents.*;
import com.resumai.agent.ai.tools.*;
import com.resumai.agent.service.ExternalProfileService;
import com.resumai.agent.service.JdRagService;
import com.resumai.agent.service.ResumeRagService;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.service.AiServices;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 8-Agent Multi-Agent Orchestrator with AiServices + MCP + Skills.
 * Implements 6-Phase DAG: Intent → Parse → JdMatch → (TechEval + ProjectEval + Risk) → EvidenceFusion → Report
 */
@Component
public class ResumeEvaluationOrchestrator {

    private static final Logger log = LoggerFactory.getLogger(ResumeEvaluationOrchestrator.class);

    private final ChatModel chatModel;
    private final ResumeRagService resumeRagService;
    private final JdRagService jdRagService;
    private final ExternalProfileService externalProfileService;
    private final SkillProvider skillProvider;
    private final McpToolRegistry mcpToolRegistry;
    private final ObjectMapper objectMapper;
    private final AgentTraceCapture traceCapture;

    private IntentAgent intentAgent;
    private ResumeParseAgent resumeParseAgent;
    private JdMatchAgent jdMatchAgent;
    private TechEvalAgent techEvalAgent;
    private ProjectEvalAgent projectEvalAgent;
    private RiskAgent riskAgent;
    private EvidenceFusionAgent evidenceFusionAgent;
    private ReportAgent reportAgent;

    private final ExecutorService parallelExecutor = Executors.newFixedThreadPool(3);

    public ResumeEvaluationOrchestrator(ChatModel chatModel,
                                         ResumeRagService resumeRagService,
                                         JdRagService jdRagService,
                                         ExternalProfileService externalProfileService,
                                         SkillProvider skillProvider,
                                         McpToolRegistry mcpToolRegistry,
                                         ObjectMapper objectMapper,
                                         AgentTraceCapture traceCapture) {
        this.chatModel = chatModel;
        this.resumeRagService = resumeRagService;
        this.jdRagService = jdRagService;
        this.externalProfileService = externalProfileService;
        this.skillProvider = skillProvider;
        this.mcpToolRegistry = mcpToolRegistry;
        this.objectMapper = objectMapper;
        this.traceCapture = traceCapture;
    }

    @PostConstruct
    public void init() {
        SkillTools skillTools = new SkillTools(skillProvider);
        ResumeParseTools parseTools = new ResumeParseTools(objectMapper);
        JdMatchTools jdMatchTools = new JdMatchTools(jdRagService, objectMapper);
        TechEvalTools techEvalTools = new TechEvalTools(resumeRagService, externalProfileService, objectMapper);
        ProjectEvalTools projectEvalTools = new ProjectEvalTools(resumeRagService, objectMapper);
        RiskTools riskTools = new RiskTools(resumeRagService, objectMapper);
        EvidenceFusionTools evidenceFusionTools = new EvidenceFusionTools(objectMapper);

        intentAgent = AiServices.builder(IntentAgent.class)
                .chatModel(chatModel)
                .tools(skillTools)
                .build();

        resumeParseAgent = AiServices.builder(ResumeParseAgent.class)
                .chatModel(chatModel)
                .tools(parseTools, skillTools)
                .build();

        jdMatchAgent = AiServices.builder(JdMatchAgent.class)
                .chatModel(chatModel)
                .tools(jdMatchTools, skillTools)
                .build();

        techEvalAgent = AiServices.builder(TechEvalAgent.class)
                .chatModel(chatModel)
                .tools(techEvalTools, skillTools)
                .toolProvider(mcpToolRegistry.asToolProvider())
                .build();

        projectEvalAgent = AiServices.builder(ProjectEvalAgent.class)
                .chatModel(chatModel)
                .tools(projectEvalTools, skillTools)
                .build();

        riskAgent = AiServices.builder(RiskAgent.class)
                .chatModel(chatModel)
                .tools(riskTools, skillTools)
                .build();

        evidenceFusionAgent = AiServices.builder(EvidenceFusionAgent.class)
                .chatModel(chatModel)
                .tools(evidenceFusionTools, skillTools)
                .build();

        reportAgent = AiServices.builder(ReportAgent.class)
                .chatModel(chatModel)
                .tools(skillTools)
                .build();

        log.info("8-Agent orchestrator initialized: AiServices + live MCP ToolProvider + Skills (6-Phase DAG)");
    }

    public EvaluationResult evaluate(String resumeText, String traceId) {
        long totalStart = System.currentTimeMillis();
        log.info("Starting 8-Agent evaluation for trace={}", traceId);
        traceCapture.begin(traceId);

        try {
            // Phase 1: Intent Routing
            String intentResult = runAgent("IntentAgent", "意图路由", 1, () ->
                    intentAgent.route(resumeText));

            // Phase 2: Resume Parse
            String parseResult = runAgent("ResumeParseAgent", "简历结构化解析", 2, () ->
                    resumeParseAgent.parse(resumeText));

            // Phase 3: JD Match
            String jdResult = runAgent("JdMatchAgent", "岗位匹配", 3, () ->
                    jdMatchAgent.matchJd(resumeText));

            // Phase 4: Parallel — TechEval + ProjectEval + Risk
            String techInput = "简历内容：\n" + truncate(resumeText, 3000) + "\n\n岗位匹配结果：\n" + truncate(jdResult, 1500);
            String riskInput = "简历内容：\n" + truncate(resumeText, 3000) + "\n\n岗位匹配信息：\n" + truncate(jdResult, 1000);
            String projectInput = "简历内容：\n" + truncate(resumeText, 3000) + "\n\n岗位要求：\n" + truncate(jdResult, 1000)
                    + "\n\n结构化解析：\n" + truncate(parseResult, 1500);

            CompletableFuture<String> techFuture = CompletableFuture.supplyAsync(() ->
                    runAgent("TechEvalAgent", "技术评估", 4, () -> techEvalAgent.evaluate(techInput)), parallelExecutor);
            CompletableFuture<String> projectFuture = CompletableFuture.supplyAsync(() ->
                    runAgent("ProjectEvalAgent", "项目深度评估", 4, () -> projectEvalAgent.evaluate(projectInput)), parallelExecutor);
            CompletableFuture<String> riskFuture = CompletableFuture.supplyAsync(() ->
                    runAgent("RiskAgent", "风险识别", 4, () -> riskAgent.analyzeRisk(riskInput)), parallelExecutor);

            CompletableFuture.allOf(techFuture, projectFuture, riskFuture).join();
            String techResult = techFuture.get();
            String projectResult = projectFuture.get();
            String riskResult = riskFuture.get();

            // Phase 5: Evidence Fusion
            String fusionInput = "技术评估结果：\n" + truncate(techResult, 1500)
                    + "\n\n项目评估结果：\n" + truncate(projectResult, 1500)
                    + "\n\n风险分析结果：\n" + truncate(riskResult, 1000)
                    + "\n\n岗位匹配：\n" + truncate(jdResult, 800);
            String fusionResult = runAgent("EvidenceFusionAgent", "证据融合", 5, () ->
                    evidenceFusionAgent.fuse(fusionInput));

            // Phase 6: Report
            String reportInput = "意图路由：\n" + truncate(intentResult, 500)
                    + "\n\n岗位匹配：\n" + truncate(jdResult, 1000)
                    + "\n\n技术评估：\n" + truncate(techResult, 1500)
                    + "\n\n项目评估：\n" + truncate(projectResult, 1500)
                    + "\n\n风险分析：\n" + truncate(riskResult, 1000)
                    + "\n\n证据融合：\n" + truncate(fusionResult, 1000);
            String finalReport = runAgent("ReportAgent", "报告生成", 6, () ->
                    reportAgent.synthesize(reportInput));

            long duration = System.currentTimeMillis() - totalStart;
            log.info("8-Agent evaluation completed in {}ms for trace={}", duration, traceId);
            traceCapture.end(traceId, "SUCCESS", duration);

            return parseEvaluationResult(finalReport, duration);
        } catch (Exception e) {
            long duration = System.currentTimeMillis() - totalStart;
            log.error("8-Agent evaluation failed for trace={}: {}", traceId, e.getMessage());
            traceCapture.end(traceId, "FAILED", duration);
            throw new RuntimeException("多Agent评估失败: " + e.getMessage(), e);
        }
    }

    private String runAgent(String agentName, String description, int phase, java.util.function.Supplier<String> execution) {
        long start = System.currentTimeMillis();
        traceCapture.agentStart(agentName, description, phase);
        AgentExecutionContext.set(traceCapture.getActiveTraceId(), agentName);
        try {
            String result = execution.get();
            long duration = System.currentTimeMillis() - start;
            traceCapture.agentEnd(agentName, "SUCCESS", duration, result);
            log.debug("Agent {} completed in {}ms", agentName, duration);
            return result;
        } catch (Exception e) {
            long duration = System.currentTimeMillis() - start;
            traceCapture.agentEnd(agentName, "FAILED", duration, "Error: " + e.getMessage());
            log.warn("Agent {} failed in {}ms: {}", agentName, duration, e.getMessage());
            return "{\"error\": \"" + agentName + " failed: " + e.getMessage() + "\"}";
        } finally {
            AgentExecutionContext.clear();
        }
    }

    private EvaluationResult parseEvaluationResult(String report, long durationMs) {
        int score = extractScore(report);
        String recommendation = extractRecommendation(report);
        List<String> strengths = extractSection(report, "核心优势");
        List<String> risks = extractSection(report, "关键风险");
        List<String> questions = extractSection(report, "面试建议问题");

        return new EvaluationResult(
                report, score, recommendation,
                strengths, risks, questions,
                null, null, null,
                8, durationMs
        );
    }

    private int extractScore(String report) {
        Matcher m = Pattern.compile("综合评分[：:]\\s*(\\d+)").matcher(report);
        return m.find() ? Integer.parseInt(m.group(1)) : 70;
    }

    private String extractRecommendation(String report) {
        Matcher m = Pattern.compile("(STRONG_RECOMMEND|RECOMMEND|NEED_MANUAL_REVIEW|NOT_RECOMMEND)")
                .matcher(report);
        return m.find() ? m.group(1) : "NEED_MANUAL_REVIEW";
    }

    private List<String> extractSection(String report, String sectionName) {
        List<String> items = new ArrayList<>();
        Pattern sectionPattern = Pattern.compile("##\\s*" + sectionName + "\\s*\\n([\\s\\S]*?)(?=\\n##|$)");
        Matcher m = sectionPattern.matcher(report);
        if (m.find()) {
            String section = m.group(1);
            Matcher itemMatcher = Pattern.compile("[-\\d]+[.)]?\\s*(.+)").matcher(section);
            while (itemMatcher.find()) {
                String item = itemMatcher.group(1).trim();
                if (!item.isEmpty()) items.add(item);
            }
        }
        return items;
    }

    private String truncate(String s, int max) {
        if (s == null) return "";
        return s.length() <= max ? s : s.substring(0, max) + "...";
    }
}
