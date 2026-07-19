package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.PolicyBundleMapper;
import com.resumai.agent.dao.PolicySelectionMapper;
import com.resumai.agent.dao.PolicyStatisticsMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicySelectionRow;
import com.resumai.agent.domain.entity.PolicyStatisticsRow;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

/**
 * Agent-level policy learning: epsilon-greedy selection over PolicyBundles
 * per task category, with Thompson Sampling once every arm has enough
 * observations. This tunes the outer loop (agent team, budgets, verification
 * rules) from HR feedback and benchmark rewards — it never trains model
 * weights.
 */
@Service
public class PolicyService {

    private static final Logger log = LoggerFactory.getLogger(PolicyService.class);
    /** Optimistic prior so unexplored arms are tried before being ruled out. */
    private static final double OPTIMISTIC_PRIOR = 0.55;
    private static final int THOMPSON_MIN_SAMPLES_PER_ARM = 5;

    private final PolicyBundleMapper bundleMapper;
    private final PolicySelectionMapper selectionMapper;
    private final PolicyStatisticsMapper statisticsMapper;
    private final ObjectMapper objectMapper;
    private final Random random = new Random();

    @Value("${resumai.policy.epsilon:0.10}")
    private double epsilon;

    public PolicyService(PolicyBundleMapper bundleMapper,
                         PolicySelectionMapper selectionMapper,
                         PolicyStatisticsMapper statisticsMapper,
                         ObjectMapper objectMapper) {
        this.bundleMapper = bundleMapper;
        this.selectionMapper = selectionMapper;
        this.statisticsMapper = statisticsMapper;
        this.objectMapper = objectMapper;
    }

    public record Selection(PolicyBundleRow bundle, String mode, double epsilonUsed) {
    }

    public List<PolicyBundleRow> listActiveBundles() {
        return bundleMapper.selectList(new QueryWrapper<PolicyBundleRow>().eq("status", "ACTIVE"));
    }

    public PolicyBundleRow getBundle(String policyId) {
        return bundleMapper.selectById(policyId);
    }

    /**
     * Pick the policy for one run and persist the selection record.
     */
    public Selection selectPolicy(String runId, String taskCategory, Map<String, Object> context) {
        List<PolicyBundleRow> candidates = listActiveBundles();
        if (candidates.isEmpty()) {
            throw new IllegalStateException("no ACTIVE policy bundles configured");
        }
        Map<String, PolicyStatisticsRow> stats = statsFor(taskCategory);
        Selection selection = choose(candidates, stats, taskCategory);
        PolicySelectionRow row = new PolicySelectionRow();
        row.setRunId(runId);
        row.setPolicyId(selection.bundle().getPolicyId());
        row.setTaskCategory(taskCategory);
        row.setSelectionMode(selection.mode());
        row.setEpsilon(BigDecimal.valueOf(selection.epsilonUsed()).setScale(3, RoundingMode.HALF_UP));
        row.setContext(writeJson(context));
        row.setCreateTime(LocalDateTime.now());
        selectionMapper.insert(row);
        log.info("policy selected run={} category={} policy={} mode={}",
                runId, taskCategory, selection.bundle().getPolicyId(), selection.mode());
        return selection;
    }

    private Selection choose(List<PolicyBundleRow> candidates,
                             Map<String, PolicyStatisticsRow> stats,
                             String taskCategory) {
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
            return new Selection(best, "THOMPSON", 0.0);
        }
        if (random.nextDouble() < epsilon) {
            PolicyBundleRow explored = candidates.get(random.nextInt(candidates.size()));
            return new Selection(explored, "EXPLORE", epsilon);
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
        return new Selection(best, "EXPLOIT", epsilon);
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

    public void markChampion(String policyId) {
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
