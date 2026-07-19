package com.resumai.agent.service.run;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.PolicyRewardMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.PolicyRewardRow;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Reward calculation for agent-level policy learning. Every component is
 * persisted individually inside {@code components}; the weighted total in
 * [0,1] feeds policy statistics. Sources: HR feedback (quality-heavy),
 * benchmark evaluators (objective) and run completion (efficiency-only,
 * low weight so unreviewed runs still teach cost/latency differences).
 */
@Service
public class RewardService {

    private static final Logger log = LoggerFactory.getLogger(RewardService.class);

    private final PolicyRewardMapper rewardMapper;
    private final PolicyService policyService;
    private final ObjectMapper objectMapper;

    public RewardService(PolicyRewardMapper rewardMapper,
                         PolicyService policyService,
                         ObjectMapper objectMapper) {
        this.rewardMapper = rewardMapper;
        this.policyService = policyService;
        this.objectMapper = objectMapper;
    }

    /** Efficiency-only reward recorded automatically when a run finishes. */
    public void recordAutoReward(AgentRun run, boolean success) {
        if (!StringUtils.hasText(run.getPolicyId())) {
            return;
        }
        Map<String, Double> components = new LinkedHashMap<>();
        JsonNode metrics = readJson(run.getMetrics());
        double llmCalls = metrics.path("llmCalls").asDouble(0);
        double toolCalls = metrics.path("toolCalls").asDouble(0);
        double latencySeconds = run.getStartedAt() != null && run.getFinishedAt() != null
                ? java.time.Duration.between(run.getStartedAt(), run.getFinishedAt()).toMillis() / 1000.0
                : 0;
        components.put("llmCost", clamp01(1.0 - llmCalls / 20.0));
        components.put("toolCost", clamp01(1.0 - toolCalls / 30.0));
        components.put("latency", clamp01(1.0 - latencySeconds / 900.0));
        components.put("failure", success ? 1.0 : 0.0);
        components.put("timeout", "TIMED_OUT".equals(run.getStatus()) ? 0.0 : 1.0);
        double total = 0.15 * components.get("llmCost")
                + 0.10 * components.get("toolCost")
                + 0.15 * components.get("latency")
                + 0.45 * components.get("failure")
                + 0.15 * components.get("timeout");
        persist(run, "AUTO", null, total * 0.3, components);
        // AUTO rewards are scaled down so a human/benchmark signal dominates.
    }

    /**
     * Quality reward from an HR feedback submission.
     *
     * @param structured optional structured payload:
     *   {accepted, recommendationAgreed, scoreDelta, missedEvidenceCount,
     *    unsupportedClaimCount, riskJudgementCorrect}
     */
    public double recordFeedbackReward(AgentRun run, Long feedbackId, int ratingScore,
                                       JsonNode structured) {
        Map<String, Double> components = new LinkedHashMap<>();
        double rating01 = clamp01((ratingScore - 1) / 4.0);
        components.put("hrAcceptance", structured != null && structured.has("accepted")
                ? (structured.path("accepted").asBoolean() ? 1.0 : 0.0)
                : rating01);
        components.put("recommendationAgreement",
                structured != null && structured.has("recommendationAgreed")
                        ? (structured.path("recommendationAgreed").asBoolean() ? 1.0 : 0.0)
                        : rating01);
        double scoreDelta = structured != null ? Math.abs(structured.path("scoreDelta").asDouble(0)) : 0;
        components.put("scoreDelta", clamp01(1.0 - scoreDelta / 30.0));
        double missedEvidence = structured != null ? structured.path("missedEvidenceCount").asDouble(0) : 0;
        components.put("missedEvidence", clamp01(1.0 - missedEvidence / 5.0));
        double unsupported = structured != null ? structured.path("unsupportedClaimCount").asDouble(0) : 0;
        components.put("unsupportedClaims", clamp01(1.0 - unsupported / 5.0));
        components.put("riskAccuracy", structured != null && structured.has("riskJudgementCorrect")
                ? (structured.path("riskJudgementCorrect").asBoolean() ? 1.0 : 0.0)
                : rating01);
        JsonNode metrics = readJson(run.getMetrics());
        components.put("evidenceSupportRatio", metrics.path("evidenceSupportRatio").asDouble(rating01));
        components.put("jdCoverage", metrics.path("jdCoverage").asDouble(rating01));
        components.put("llmCost", clamp01(1.0 - metrics.path("llmCalls").asDouble(0) / 20.0));
        components.put("latency", clamp01(1.0 - metrics.path("latencySeconds").asDouble(0) / 900.0));

        double total = 0.22 * components.get("hrAcceptance")
                + 0.14 * components.get("recommendationAgreement")
                + 0.10 * components.get("scoreDelta")
                + 0.10 * components.get("missedEvidence")
                + 0.12 * components.get("unsupportedClaims")
                + 0.08 * components.get("riskAccuracy")
                + 0.10 * components.get("evidenceSupportRatio")
                + 0.06 * components.get("jdCoverage")
                + 0.04 * components.get("llmCost")
                + 0.04 * components.get("latency");
        persist(run, "FEEDBACK", feedbackId, total, components);
        return total;
    }

    /** Objective reward reported by the benchmark evaluator. */
    public void recordBenchmarkReward(String runId, String policyId, String taskCategory,
                                      double total, Map<String, Double> components) {
        PolicyRewardRow row = new PolicyRewardRow();
        row.setRunId(runId);
        row.setPolicyId(policyId);
        row.setTaskCategory(taskCategory);
        row.setSource("BENCHMARK");
        row.setTotalReward(BigDecimal.valueOf(clamp01(total)).setScale(4, RoundingMode.HALF_UP));
        row.setComponents(writeJson(components));
        row.setCreateTime(LocalDateTime.now());
        rewardMapper.insert(row);
        policyService.recordReward(policyId, taskCategory, clamp01(total));
    }

    private void persist(AgentRun run, String source, Long feedbackId,
                         double total, Map<String, Double> components) {
        if (!StringUtils.hasText(run.getPolicyId())) {
            log.info("run {} has no policy, reward skipped", run.getRunId());
            return;
        }
        String category = StringUtils.hasText(run.getRunType()) ? run.getRunType() : "unknown";
        PolicyRewardRow row = new PolicyRewardRow();
        row.setRunId(run.getRunId());
        row.setPolicyId(run.getPolicyId());
        row.setTaskCategory(category);
        row.setSource(source);
        row.setFeedbackId(feedbackId);
        row.setTotalReward(BigDecimal.valueOf(clamp01(total)).setScale(4, RoundingMode.HALF_UP));
        row.setComponents(writeJson(components));
        row.setCreateTime(LocalDateTime.now());
        rewardMapper.insert(row);
        policyService.recordReward(run.getPolicyId(), category, clamp01(total));
    }

    private double clamp01(double value) {
        return Math.max(0.0, Math.min(1.0, value));
    }

    private JsonNode readJson(String json) {
        try {
            return json != null ? objectMapper.readTree(json)
                    : objectMapper.createObjectNode();
        } catch (Exception e) {
            return objectMapper.createObjectNode();
        }
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "{}";
        }
    }
}
