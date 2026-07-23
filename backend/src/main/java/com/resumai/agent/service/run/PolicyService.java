package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.PolicyBundleMapper;
import com.resumai.agent.dao.PolicyChampionAssignmentMapper;
import com.resumai.agent.dao.PolicySelectionMapper;
import com.resumai.agent.dao.PolicyStatisticsMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicyChampionAssignmentRow;
import com.resumai.agent.domain.entity.PolicySelectionRow;
import com.resumai.agent.domain.entity.PolicyStatisticsRow;
import com.resumai.agent.domain.enums.PolicySelectionMode;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Agent-level policy selector for the Policy Optimization Lab（无 GPU）.
 *
 * <ul>
 *   <li>{@link ExecutionPurpose#PRODUCTION_DECISION} — champion assignment only; no epsilon /
 *       Thompson exploration on real candidate traffic.</li>
 *   <li>{@link ExecutionPurpose#SHADOW_EXPERIMENT} / {@link ExecutionPurpose#LAB_EXPERIMENT}
 *       — epsilon-greedy / Thompson bandit over eligible ACTIVE arms (lab/shadow only).</li>
 * </ul>
 *
 * Never trains model weights ({@code MODEL_WEIGHTS: unchanged}).
 *
 * <p>Eligibility is driven by {@code supportedRunTypes} on each bundle config.
 * Thin policies such as {@code low_cost} must not be selected for
 * {@code full_evaluation} — raising their LLM budget is not a substitute.
 */
@Service
public class PolicyService {

    private static final Logger log = LoggerFactory.getLogger(PolicyService.class);
    /** Optimistic prior so unexplored arms are tried before being ruled out. */
    private static final double OPTIMISTIC_PRIOR = 0.55;
    private static final int THOMPSON_MIN_SAMPLES_PER_ARM = 5;

    /** Hard-coded denylist when config lacks supportedRunTypes (legacy rows). */
    private static final Map<String, Set<String>> DEFAULT_UNSUPPORTED = Map.of(
            "low_cost", Set.of(
                    "full_evaluation", "jd_evaluation", "backend_eval", "agent_eval",
                    "project_analysis", "project_rewrite", "resume_optimize",
                    "interview_questions", "jd_gap"),
            "resume_rewrite", Set.of(
                    "full_evaluation", "jd_evaluation", "backend_eval", "agent_eval",
                    "tech_match", "risk_check", "timeline_check", "evidence_check",
                    "followup", "quick_answer", "jd_gap", "interview_questions")
    );

    private final PolicyBundleMapper bundleMapper;
    private final PolicySelectionMapper selectionMapper;
    private final PolicyStatisticsMapper statisticsMapper;
    private final PolicyChampionAssignmentMapper championAssignmentMapper;
    private final ObjectMapper objectMapper;
    private final Random random = new Random();

    @Value("${resumai.policy.epsilon:0.10}")
    private double epsilon;

    public PolicyService(PolicyBundleMapper bundleMapper,
                         PolicySelectionMapper selectionMapper,
                         PolicyStatisticsMapper statisticsMapper,
                         PolicyChampionAssignmentMapper championAssignmentMapper,
                         ObjectMapper objectMapper) {
        this.bundleMapper = bundleMapper;
        this.selectionMapper = selectionMapper;
        this.statisticsMapper = statisticsMapper;
        this.championAssignmentMapper = championAssignmentMapper;
        this.objectMapper = objectMapper;
    }

    /**
     * Why a policy is being selected. Production never explores; bandit arms
     * are reserved for shadow/lab traffic that must not write back to candidates.
     */
    public enum ExecutionPurpose {
        PRODUCTION_DECISION,
        SHADOW_EXPERIMENT,
        LAB_EXPERIMENT
    }

    public record Selection(
            PolicyBundleRow bundle,
            PolicySelectionMode mode,
            double epsilonUsed,
            Integer assignmentVersion
    ) {
    }

    public List<PolicyBundleRow> listActiveBundles() {
        return bundleMapper.selectList(new QueryWrapper<PolicyBundleRow>().eq("status", "ACTIVE"));
    }

    public PolicyBundleRow getBundle(String policyId) {
        return bundleMapper.selectById(policyId);
    }

    /**
     * Benchmark/replay path: the caller pinned an exact policy. Recorded as a
     * FORCED selection so learning statistics can exclude or analyse it.
     */
    public Selection forcedSelection(String runId, String taskCategory, String policyId) {
        PolicyBundleRow bundle = bundleMapper.selectById(policyId);
        if (bundle == null) {
            throw new IllegalStateException("forced policy not found: " + policyId);
        }
        Selection selection = new Selection(bundle, PolicySelectionMode.FORCED, 0.0, null);
        PolicySelectionRow row = new PolicySelectionRow();
        row.setRunId(runId);
        row.setPolicyId(policyId);
        row.setTaskCategory(taskCategory);
        row.setSelectionMode(PolicySelectionMode.FORCED.storageValue());
        row.setEpsilon(BigDecimal.ZERO.setScale(3, RoundingMode.HALF_UP));
        row.setContext("{\"forced\":true}");
        row.setCreateTime(LocalDateTime.now());
        persistSelection(runId, row);
        return selection;
    }

    /**
     * Production path: champion-only (no epsilon exploration).
     */
    public Selection selectPolicy(String runId, String taskCategory, Map<String, Object> context) {
        return selectPolicy(runId, taskCategory, context, ExecutionPurpose.PRODUCTION_DECISION);
    }

    /**
     * Shadow / Policy Lab path: epsilon-greedy or Thompson over eligible arms.
     * Must not be used for candidate-facing production decisions.
     */
    public Selection selectForShadowOrLab(String runId, String taskCategory,
                                          Map<String, Object> context,
                                          ExecutionPurpose purpose) {
        if (purpose == null || purpose == ExecutionPurpose.PRODUCTION_DECISION) {
            throw new IllegalArgumentException(
                    "shadow/lab selection requires SHADOW_EXPERIMENT or LAB_EXPERIMENT");
        }
        return selectPolicy(runId, taskCategory, context, purpose);
    }

    /**
     * Pick the policy for one run and persist the selection record.
     */
    public Selection selectPolicy(String runId, String taskCategory,
                                  Map<String, Object> context,
                                  ExecutionPurpose purpose) {
        ExecutionPurpose resolved = purpose != null
                ? purpose : ExecutionPurpose.PRODUCTION_DECISION;
        List<PolicyBundleRow> candidates = listActiveBundles();
        if (candidates.isEmpty()) {
            throw new IllegalStateException("no ACTIVE policy bundles configured");
        }
        String runType = resolveRunType(taskCategory, context);
        candidates = filterForTask(candidates, taskCategory, context);
        if (candidates.isEmpty()) {
            // Do NOT reopen ineligible arms (e.g. low_cost for full_evaluation).
            log.warn("no eligible policy for runType={}, falling back to balanced/any eligible",
                    runType);
            candidates = listActiveBundles().stream()
                    .filter(b -> supportsRunType(b, runType))
                    .toList();
            if (candidates.isEmpty()) {
                PolicyBundleRow balanced = bundleMapper.selectById("balanced");
                if (balanced != null && "ACTIVE".equalsIgnoreCase(balanced.getStatus())) {
                    candidates = List.of(balanced);
                } else {
                    candidates = listActiveBundles();
                }
            }
        }
        Map<String, PolicyStatisticsRow> stats = statsFor(taskCategory);
        Selection selection = resolved == ExecutionPurpose.PRODUCTION_DECISION
                ? chooseProductionChampion(runType, cohortKey(context), candidates)
                : chooseBandit(candidates, stats);
        Map<String, Object> recorded = context != null
                ? new HashMap<>(context) : new HashMap<>();
        recorded.put("executionPurpose", resolved.name());
        if (selection.assignmentVersion() != null) {
            recorded.put("assignmentVersion", selection.assignmentVersion());
        }
        PolicySelectionRow row = new PolicySelectionRow();
        row.setRunId(runId);
        row.setPolicyId(selection.bundle().getPolicyId());
        row.setTaskCategory(taskCategory);
        row.setSelectionMode(selection.mode().storageValue());
        row.setEpsilon(BigDecimal.valueOf(selection.epsilonUsed()).setScale(3, RoundingMode.HALF_UP));
        row.setContext(writeJson(recorded));
        row.setCreateTime(LocalDateTime.now());
        persistSelection(runId, row);
        log.info("policy selected run={} category={} policy={} mode={} purpose={}",
                runId, taskCategory, selection.bundle().getPolicyId(),
                selection.mode().storageValue(), resolved);
        return selection;
    }

    private void persistSelection(String runId, PolicySelectionRow row) {
        try {
            selectionMapper.insert(row);
        } catch (DataAccessException e) {
            throw new PolicySelectionPersistenceException(runId, row.getSelectionMode(), e);
        }
    }

    static String cohortKey(Map<String, Object> context) {
        if (context != null) {
            Object raw = context.get("cohortKey");
            if (raw == null) {
                raw = context.get("cohort");
            }
            if (raw != null && StringUtils.hasText(String.valueOf(raw))
                    && !"null".equalsIgnoreCase(String.valueOf(raw))) {
                return String.valueOf(raw).trim();
            }
        }
        return "default";
    }

    /**
     * Run-type eligibility filter. Policies advertise {@code supportedRunTypes};
     * {@code low_cost} never accepts {@code full_evaluation}.
     */
    List<PolicyBundleRow> filterForTask(List<PolicyBundleRow> candidates,
                                        String taskCategory,
                                        Map<String, Object> context) {
        String runType = resolveRunType(taskCategory, context);
        List<PolicyBundleRow> filtered = new ArrayList<>();
        for (PolicyBundleRow candidate : candidates) {
            if (!supportsRunType(candidate, runType)) {
                log.debug("skipping policy {} unsupported for runType={}",
                        candidate.getPolicyId(), runType);
                continue;
            }
            filtered.add(candidate);
        }
        return filtered;
    }

    boolean supportsRunType(PolicyBundleRow bundle, String runType) {
        if (bundle == null || !StringUtils.hasText(runType)) {
            return true;
        }
        String policyId = bundle.getPolicyId() != null ? bundle.getPolicyId() : "";
        Set<String> supported = readSupportedRunTypes(bundle);
        if (!supported.isEmpty()) {
            return supported.contains(runType);
        }
        // Legacy rows without supportedRunTypes: apply hard denylist.
        Set<String> unsupported = DEFAULT_UNSUPPORTED.getOrDefault(policyId, Set.of());
        return !unsupported.contains(runType);
    }

    private Set<String> readSupportedRunTypes(PolicyBundleRow bundle) {
        Set<String> out = new HashSet<>();
        try {
            if (bundle.getConfig() == null || bundle.getConfig().isBlank()) {
                return out;
            }
            JsonNode node = objectMapper.readTree(bundle.getConfig());
            JsonNode arr = node.get("supportedRunTypes");
            if (arr != null && arr.isArray()) {
                for (JsonNode item : arr) {
                    if (item != null && item.isTextual() && StringUtils.hasText(item.asText())) {
                        out.add(item.asText().trim());
                    }
                }
            }
        } catch (Exception ignored) {
            // best-effort eligibility
        }
        return out;
    }

    static String resolveRunType(String taskCategory, Map<String, Object> context) {
        if (context != null) {
            Object fromCtx = context.get("runType");
            if (fromCtx == null) {
                fromCtx = context.get("goalCategory");
            }
            if (fromCtx != null && StringUtils.hasText(String.valueOf(fromCtx))
                    && !"null".equalsIgnoreCase(String.valueOf(fromCtx))) {
                return String.valueOf(fromCtx).trim();
            }
        }
        if (StringUtils.hasText(taskCategory)) {
            return taskCategory.trim();
        }
        return "full_evaluation";
    }

    /**
     * Production: active champion assignment among eligible arms. No random exploration.
     * Falls back to {@code balanced} with {@link PolicySelectionMode#FALLBACK} — never a
     * long label like {@code CHAMPION_FALLBACK}.
     */
    Selection chooseProductionChampion(String runType, String cohortKey,
                                       List<PolicyBundleRow> eligible) {
        if (eligible == null || eligible.isEmpty()) {
            throw new IllegalStateException("no eligible policies for production decision");
        }
        String cohort = StringUtils.hasText(cohortKey) ? cohortKey.trim() : "default";
        PolicyChampionAssignmentRow assignment = findActiveAssignment(runType, cohort);
        if (assignment != null && StringUtils.hasText(assignment.getPolicyId())) {
            PolicyBundleRow assigned = eligible.stream()
                    .filter(p -> assignment.getPolicyId().equals(p.getPolicyId()))
                    .findFirst()
                    .orElse(null);
            if (assigned != null && supportsRunType(assigned, runType)) {
                return new Selection(assigned, PolicySelectionMode.CHAMPION, 0.0,
                        assignment.getVersion());
            }
            log.warn("assigned champion {} ineligible for runType={}, falling back",
                    assignment.getPolicyId(), runType);
        }
        for (PolicyBundleRow candidate : eligible) {
            if ("balanced".equals(candidate.getPolicyId())) {
                return new Selection(candidate, PolicySelectionMode.FALLBACK, 0.0, null);
            }
        }
        return new Selection(eligible.get(0), PolicySelectionMode.FALLBACK, 0.0, null);
    }

    /**
     * Legacy flag-based champion pick (tests / bandit tie-break helpers).
     * Prefer {@link #chooseProductionChampion} for production traffic.
     */
    Selection chooseChampion(List<PolicyBundleRow> candidates) {
        if (candidates == null || candidates.isEmpty()) {
            throw new IllegalStateException("no eligible policies for production decision");
        }
        for (PolicyBundleRow candidate : candidates) {
            if (candidate.getIsChampion() != null && candidate.getIsChampion() == 1) {
                return new Selection(candidate, PolicySelectionMode.CHAMPION, 0.0, null);
            }
        }
        for (PolicyBundleRow candidate : candidates) {
            if ("balanced".equals(candidate.getPolicyId())) {
                return new Selection(candidate, PolicySelectionMode.FALLBACK, 0.0, null);
            }
        }
        return new Selection(candidates.get(0), PolicySelectionMode.FALLBACK, 0.0, null);
    }

    /**
     * Shadow/lab bandit: Thompson when every arm has enough samples, else
     * epsilon-greedy. Never called for PRODUCTION_DECISION.
     */
    Selection chooseBandit(List<PolicyBundleRow> candidates,
                           Map<String, PolicyStatisticsRow> stats) {
        boolean thompsonReady = candidates.stream().allMatch(c -> {
            PolicyStatisticsRow s = stats.get(c.getPolicyId());
            return s != null && s.getRewardCount() != null
                    && s.getRewardCount() >= THOMPSON_MIN_SAMPLES_PER_ARM;
        });
        if (thompsonReady) {
            PolicyBundleRow best = null;
            double bestSample = -Double.MAX_VALUE;
            for (PolicyBundleRow candidate : candidates) {
                PolicyStatisticsRow s = stats.get(candidate.getPolicyId());
                double mean = s.getAvgReward().doubleValue();
                int n = s.getRewardCount();
                double sqSum = s.getRewardSqSum().doubleValue();
                double variance = Math.max(1e-4, sqSum / n - mean * mean);
                double sample = mean + random.nextGaussian() * Math.sqrt(variance / n);
                if (sample > bestSample) {
                    bestSample = sample;
                    best = candidate;
                }
            }
            return new Selection(best, PolicySelectionMode.THOMPSON, 0.0, null);
        }
        if (random.nextDouble() < epsilon) {
            PolicyBundleRow explored = candidates.get(random.nextInt(candidates.size()));
            return new Selection(explored, PolicySelectionMode.EXPLORE, epsilon, null);
        }
        PolicyBundleRow best = null;
        double bestScore = -Double.MAX_VALUE;
        for (PolicyBundleRow candidate : candidates) {
            PolicyStatisticsRow s = stats.get(candidate.getPolicyId());
            double score = s != null && s.getRewardCount() != null && s.getRewardCount() > 0
                    ? s.getAvgReward().doubleValue()
                    : OPTIMISTIC_PRIOR;
            if (candidate.getIsChampion() != null && candidate.getIsChampion() == 1) {
                score += 0.001; // deterministic tie-break for the current champion
            }
            if (score > bestScore) {
                bestScore = score;
                best = candidate;
            }
        }
        return new Selection(best, PolicySelectionMode.EXPLOIT, epsilon, null);
    }

    private PolicyChampionAssignmentRow findActiveAssignment(String runType, String cohortKey) {
        if (championAssignmentMapper == null || !StringUtils.hasText(runType)) {
            return null;
        }
        try {
            return championAssignmentMapper.selectOne(
                    new QueryWrapper<PolicyChampionAssignmentRow>()
                            .eq("run_type", runType)
                            .eq("cohort_key", cohortKey)
                            .eq("active", 1)
                            .last("limit 1"));
        } catch (Exception e) {
            log.debug("champion assignment lookup failed runType={}: {}", runType, e.getMessage());
            return null;
        }
    }

    private Map<String, PolicyStatisticsRow> statsFor(String taskCategory) {
        List<PolicyStatisticsRow> rows = statisticsMapper.selectList(
                new QueryWrapper<PolicyStatisticsRow>().eq("task_category", taskCategory));
        Map<String, PolicyStatisticsRow> byPolicy = new HashMap<>();
        for (PolicyStatisticsRow row : rows) {
            byPolicy.put(row.getPolicyId(), row);
        }
        return byPolicy;
    }

    /** Record a completed run (success/failure counting, run_count). */
    public synchronized void recordRunOutcome(String policyId, String taskCategory, boolean success) {
        PolicyStatisticsRow row = ensureRow(policyId, taskCategory);
        row.setRunCount(nz(row.getRunCount()) + 1);
        if (success) {
            row.setSuccessCount(nz(row.getSuccessCount()) + 1);
        } else {
            row.setFailureCount(nz(row.getFailureCount()) + 1);
        }
        row.setUpdateTime(LocalDateTime.now());
        statisticsMapper.updateById(row);
    }

    /** Fold one reward observation into the running statistics. */
    public synchronized void recordReward(String policyId, String taskCategory, double reward) {
        PolicyStatisticsRow row = ensureRow(policyId, taskCategory);
        int count = nz(row.getRewardCount()) + 1;
        BigDecimal total = row.getTotalReward().add(BigDecimal.valueOf(reward));
        row.setRewardCount(count);
        row.setTotalReward(total.setScale(4, RoundingMode.HALF_UP));
        row.setAvgReward(total.divide(BigDecimal.valueOf(count), 4, RoundingMode.HALF_UP));
        row.setRewardSqSum(row.getRewardSqSum()
                .add(BigDecimal.valueOf(reward * reward)).setScale(6, RoundingMode.HALF_UP));
        row.setUpdateTime(LocalDateTime.now());
        statisticsMapper.updateById(row);
    }

    /**
     * Legacy global champion flip (kept for evolution verdict). Prefer
     * {@link #assignChampion} for per-runType production traffic.
     */
    public void markChampion(String policyId) {
        updateIsChampionFlags(policyId);
    }

    /**
     * Upsert the active champion assignment for a runType/cohort and keep
     * {@code is_champion} consistent for the full_evaluation default cohort.
     * Rejects policies that do not support the target runType.
     */
    public void assignChampion(String runType, String cohortKey, String policyId,
                               String actor, String experimentId) {
        if (!StringUtils.hasText(runType) || !StringUtils.hasText(policyId)) {
            throw new IllegalArgumentException("runType and policyId are required");
        }
        PolicyBundleRow bundle = bundleMapper.selectById(policyId);
        if (bundle == null) {
            throw new IllegalArgumentException("policy not found: " + policyId);
        }
        if (!supportsRunType(bundle, runType)) {
            throw new IllegalArgumentException(
                    "policy " + policyId + " does not support runType " + runType);
        }
        String cohort = StringUtils.hasText(cohortKey) ? cohortKey.trim() : "default";
        String approvedBy = StringUtils.hasText(actor) ? actor.trim() : "system";
        LocalDateTime now = LocalDateTime.now();
        PolicyChampionAssignmentRow existing = findActiveAssignment(runType, cohort);
        if (existing != null) {
            existing.setPolicyId(policyId);
            existing.setExperimentId(experimentId);
            existing.setApprovedBy(approvedBy);
            existing.setApprovedAt(now);
            existing.setVersion(existing.getVersion() != null ? existing.getVersion() + 1 : 1);
            championAssignmentMapper.updateById(existing);
        } else {
            PolicyChampionAssignmentRow row = new PolicyChampionAssignmentRow();
            row.setRunType(runType);
            row.setCohortKey(cohort);
            row.setPolicyId(policyId);
            row.setExperimentId(experimentId);
            row.setApprovedBy(approvedBy);
            row.setApprovedAt(now);
            row.setVersion(1);
            row.setActive(1);
            championAssignmentMapper.insert(row);
        }
        if ("full_evaluation".equals(runType) && "default".equals(cohort)) {
            updateIsChampionFlags(policyId);
        }
    }

    private void updateIsChampionFlags(String policyId) {
        List<PolicyBundleRow> all = bundleMapper.selectList(null);
        for (PolicyBundleRow bundle : all) {
            int flag = bundle.getPolicyId().equals(policyId) ? 1 : 0;
            if (bundle.getIsChampion() == null || bundle.getIsChampion() != flag) {
                bundle.setIsChampion(flag);
                bundleMapper.updateById(bundle);
            }
        }
    }

    public List<PolicyStatisticsRow> listStatistics(String taskCategory) {
        QueryWrapper<PolicyStatisticsRow> query = new QueryWrapper<>();
        if (taskCategory != null && !taskCategory.isBlank()) {
            query.eq("task_category", taskCategory);
        }
        return statisticsMapper.selectList(query.orderByDesc("avg_reward"));
    }

    private PolicyStatisticsRow ensureRow(String policyId, String taskCategory) {
        PolicyStatisticsRow row = statisticsMapper.selectOne(new QueryWrapper<PolicyStatisticsRow>()
                .eq("policy_id", policyId).eq("task_category", taskCategory).last("limit 1"));
        if (row != null) {
            return row;
        }
        row = new PolicyStatisticsRow();
        row.setPolicyId(policyId);
        row.setTaskCategory(taskCategory);
        row.setRunCount(0);
        row.setRewardCount(0);
        row.setTotalReward(BigDecimal.ZERO);
        row.setAvgReward(BigDecimal.ZERO);
        row.setRewardSqSum(BigDecimal.ZERO);
        row.setSuccessCount(0);
        row.setFailureCount(0);
        row.setUpdateTime(LocalDateTime.now());
        try {
            statisticsMapper.insert(row);
        } catch (org.springframework.dao.DuplicateKeyException raced) {
            row = statisticsMapper.selectOne(new QueryWrapper<PolicyStatisticsRow>()
                    .eq("policy_id", policyId).eq("task_category", taskCategory).last("limit 1"));
        }
        return row;
    }

    private int nz(Integer value) {
        return value != null ? value : 0;
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value != null ? value : Map.of());
        } catch (Exception e) {
            return "{}";
        }
    }

    /** Test hook. */
    void setEpsilon(double epsilon) {
        this.epsilon = epsilon;
    }
}
