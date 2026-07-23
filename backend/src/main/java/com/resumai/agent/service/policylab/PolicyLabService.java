package com.resumai.agent.service.policylab;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.ApiConflictException;
import com.resumai.agent.api.ApiNotFoundException;
import com.resumai.agent.api.dto.policylab.CreatePolicyExperimentRequest;
import com.resumai.agent.api.dto.policylab.HardGateView;
import com.resumai.agent.api.dto.policylab.PolicyCandidateView;
import com.resumai.agent.api.dto.policylab.PolicyExperimentDetailResponse;
import com.resumai.agent.api.dto.policylab.PolicyExperimentView;
import com.resumai.agent.api.dto.policylab.PolicyTrialView;
import com.resumai.agent.api.dto.policylab.PromotionView;
import com.resumai.agent.api.dto.policylab.SandboxDiagnosticView;
import com.resumai.agent.dao.PolicyCandidateMapper;
import com.resumai.agent.dao.PolicyChampionAssignmentMapper;
import com.resumai.agent.dao.PolicyExperimentMapper;
import com.resumai.agent.dao.PolicyPromotionMapper;
import com.resumai.agent.dao.PolicyTrialMapper;
import com.resumai.agent.dao.SandboxExecutionMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicyCandidate;
import com.resumai.agent.domain.entity.PolicyChampionAssignmentRow;
import com.resumai.agent.domain.entity.PolicyExperiment;
import com.resumai.agent.domain.entity.PolicyPromotion;
import com.resumai.agent.domain.entity.PolicyTrial;
import com.resumai.agent.domain.entity.SandboxExecutionRow;
import com.resumai.agent.service.run.PolicyService;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

@Service
public class PolicyLabService {

    private final PolicyExperimentMapper experimentMapper;
    private final PolicyCandidateMapper candidateMapper;
    private final PolicyTrialMapper trialMapper;
    private final PolicyPromotionMapper promotionMapper;
    private final PolicyChampionAssignmentMapper championAssignmentMapper;
    private final SandboxExecutionMapper sandboxExecutionMapper;
    private final PolicyService policyService;
    private final PolicyLabEventService eventService;
    private final PolicyLabEvaluator evaluator;
    private final ObjectMapper objectMapper;

    public PolicyLabService(PolicyExperimentMapper experimentMapper,
                            PolicyCandidateMapper candidateMapper,
                            PolicyTrialMapper trialMapper,
                            PolicyPromotionMapper promotionMapper,
                            PolicyChampionAssignmentMapper championAssignmentMapper,
                            SandboxExecutionMapper sandboxExecutionMapper,
                            PolicyService policyService,
                            PolicyLabEventService eventService,
                            PolicyLabEvaluator evaluator,
                            ObjectMapper objectMapper) {
        this.experimentMapper = experimentMapper;
        this.candidateMapper = candidateMapper;
        this.trialMapper = trialMapper;
        this.promotionMapper = promotionMapper;
        this.championAssignmentMapper = championAssignmentMapper;
        this.sandboxExecutionMapper = sandboxExecutionMapper;
        this.policyService = policyService;
        this.eventService = eventService;
        this.evaluator = evaluator;
        this.objectMapper = objectMapper;
    }

    @Transactional
    public PolicyExperimentView create(CreatePolicyExperimentRequest request, String actor) {
        requireText(request.basePolicyId(), "basePolicyId");
        requireText(request.evalDataset(), "evalDataset");
        requireText(request.gateDataset(), "gateDataset");
        requireText(request.safetyDataset(), "safetyDataset");
        if (request.seeds() == null || request.seeds().isEmpty()) {
            throw new IllegalArgumentException("seeds required");
        }
        if (request.repeatsPerCase() < 1) {
            throw new IllegalArgumentException("repeatsPerCase must be >= 1");
        }
        if (request.caseLimit() == null || request.caseLimit() < 1) {
            throw new IllegalArgumentException("caseLimit required");
        }
        if (request.budgetCny() == null || request.budgetCny().compareTo(BigDecimal.ZERO) <= 0) {
            throw new IllegalArgumentException("budgetCny required");
        }
        PolicyBundleRow base = policyService.getBundle(request.basePolicyId());
        if (base == null) {
            throw new ApiNotFoundException("base policy not found: " + request.basePolicyId());
        }

        String experimentId = "pexp-" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        LocalDateTime now = LocalDateTime.now();
        PolicyExperiment row = new PolicyExperiment();
        row.setExperimentId(experimentId);
        row.setKind(StringUtils.hasText(request.kind()) ? request.kind() : "OFFLINE_SEARCH");
        row.setStatus("PENDING");
        row.setGeneration(0);
        row.setBasePolicyId(request.basePolicyId());
        row.setChampionPolicyId(request.basePolicyId());
        row.setRunType(StringUtils.hasText(request.runType()) ? request.runType() : "full_evaluation");
        row.setCohortKey(StringUtils.hasText(request.cohortKey()) ? request.cohortKey() : "default");
        row.setEvalDataset(request.evalDataset());
        row.setGateDataset(request.gateDataset());
        row.setSafetyDataset(request.safetyDataset());
        row.setSeedsJson(writeJson(request.seeds()));
        row.setRepeatsPerCase(request.repeatsPerCase());
        row.setCaseLimit(request.caseLimit());
        row.setBudgetCny(request.budgetCny());
        row.setSpentCny(BigDecimal.ZERO);
        row.setProgressPct(BigDecimal.ZERO);
        row.setProgressPhase("CREATED");
        row.setPauseRequested(0);
        row.setCancelRequested(0);
        // First version: NEVER honor client autoPromote — always force false.
        row.setAutoPromote(0);
        row.setNote(request.note());
        row.setCreatedBy(StringUtils.hasText(actor) ? actor : "developer");
        row.setCreateTime(now);
        row.setUpdateTime(now);
        experimentMapper.insert(row);

        eventService.emit(experimentId, "EXPERIMENT_CREATED", Map.of(
                "basePolicyId", request.basePolicyId(),
                "autoPromote", false,
                "actor", row.getCreatedBy()));
        return toView(row);
    }

    public List<PolicyExperimentView> list(int limit) {
        int cap = Math.max(1, Math.min(limit, 200));
        return experimentMapper.selectList(
                        new QueryWrapper<PolicyExperiment>().orderByDesc("create_time").last("limit " + cap))
                .stream().map(this::toView).toList();
    }

    public PolicyExperimentDetailResponse getDetail(String experimentId) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        List<PolicyCandidate> candidates = candidateMapper.selectList(
                new QueryWrapper<PolicyCandidate>().eq("experiment_id", experimentId)
                        .orderByAsc("create_time"));
        List<PolicyTrial> trials = trialMapper.selectList(
                new QueryWrapper<PolicyTrial>().eq("experiment_id", experimentId)
                        .orderByAsc("create_time"));
        List<HardGateView> hardGates = new ArrayList<>();
        for (PolicyCandidate candidate : candidates) {
            PolicyLabEvaluator.GateReport report = evaluator.evaluateStored(experiment, candidate, trials);
            hardGates.addAll(report.hardGates());
        }
        List<SandboxDiagnosticView> sandboxes = sandboxExecutionMapper.selectList(
                        new QueryWrapper<SandboxExecutionRow>()
                                .eq("experiment_id", experimentId)
                                .orderByDesc("create_time")
                                .last("limit 100"))
                .stream().map(this::toSandboxView).toList();
        return new PolicyExperimentDetailResponse(
                toView(experiment),
                candidates.stream().map(this::toCandidateView).toList(),
                trials.stream().map(this::toTrialView).toList(),
                hardGates,
                sandboxes,
                actionsAllowed(experiment, candidates));
    }

    @Transactional
    public PolicyExperimentView requestPause(String experimentId, String actor) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        assertMutable(experiment);
        experiment.setPauseRequested(1);
        if ("RUNNING".equalsIgnoreCase(experiment.getStatus())) {
            experiment.setStatus("PAUSED");
            experiment.setProgressPhase("PAUSED");
        }
        experiment.setUpdateTime(LocalDateTime.now());
        experimentMapper.updateById(experiment);
        eventService.emit(experimentId, "PAUSE_REQUESTED", Map.of("actor", nz(actor)));
        return toView(experiment);
    }

    @Transactional
    public PolicyExperimentView resume(String experimentId, String actor) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        if (!"PAUSED".equalsIgnoreCase(experiment.getStatus())
                && (experiment.getPauseRequested() == null || experiment.getPauseRequested() == 0)) {
            throw new ApiConflictException("experiment is not paused: " + experiment.getStatus());
        }
        if (flag(experiment.getCancelRequested())) {
            throw new ApiConflictException("experiment cancel already requested");
        }
        experiment.setPauseRequested(0);
        experiment.setStatus("RUNNING");
        experiment.setProgressPhase("RUNNING");
        if (experiment.getStartedAt() == null) {
            experiment.setStartedAt(LocalDateTime.now());
        }
        experiment.setUpdateTime(LocalDateTime.now());
        experimentMapper.updateById(experiment);
        eventService.emit(experimentId, "RESUMED", Map.of("actor", nz(actor)));
        return toView(experiment);
    }

    @Transactional
    public PolicyExperimentView requestCancel(String experimentId, String actor) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        assertMutable(experiment);
        experiment.setCancelRequested(1);
        experiment.setStatus("CANCELLED");
        experiment.setProgressPhase("CANCELLED");
        experiment.setFinishedAt(LocalDateTime.now());
        experiment.setUpdateTime(LocalDateTime.now());
        experimentMapper.updateById(experiment);
        eventService.emit(experimentId, "CANCEL_REQUESTED", Map.of("actor", nz(actor)));
        return toView(experiment);
    }

    @Transactional
    public PolicyExperimentView cloneForRerun(String experimentId, String actor) {
        PolicyExperiment source = requireExperiment(experimentId);
        CreatePolicyExperimentRequest request = new CreatePolicyExperimentRequest(
                source.getKind(),
                source.getBasePolicyId(),
                source.getRunType(),
                source.getCohortKey(),
                source.getEvalDataset(),
                source.getGateDataset(),
                source.getSafetyDataset(),
                readSeeds(source.getSeedsJson()),
                source.getRepeatsPerCase() != null ? source.getRepeatsPerCase() : 1,
                source.getCaseLimit() != null ? source.getCaseLimit() : 1,
                source.getBudgetCny(),
                "rerun of " + experimentId + (source.getNote() != null ? ": " + source.getNote() : ""),
                false);
        PolicyExperimentView cloned = create(request, actor);
        eventService.emit(cloned.experimentId(), "CLONED_FOR_RERUN", Map.of(
                "sourceExperimentId", experimentId,
                "actor", nz(actor)));
        return cloned;
    }

    @Transactional
    public PromotionView promote(String candidateId, String reason, String actor) {
        PolicyCandidate candidate = candidateMapper.selectById(candidateId);
        if (candidate == null) {
            throw new ApiNotFoundException("candidate not found: " + candidateId);
        }
        if (!"PASSED_GATE".equalsIgnoreCase(candidate.getStatus())) {
            throw new ApiConflictException("candidate has not passed hard gates");
        }
        if (!StringUtils.hasText(candidate.getBundlePolicyId())) {
            throw new ApiConflictException("candidate missing bundlePolicyId");
        }
        PolicyExperiment experiment = requireExperiment(candidate.getExperimentId());
        List<PolicyTrial> trials = trialMapper.selectList(
                new QueryWrapper<PolicyTrial>().eq("candidate_id", candidateId));
        PolicyLabEvaluator.GateReport gateReport =
                evaluator.evaluateStored(experiment, candidate, trials);
        if (!gateReport.allPassed()) {
            throw new ApiConflictException("hard gates failed");
        }

        String previous = currentChampionPolicyId(experiment.getRunType(), experiment.getCohortKey());
        policyService.assignChampion(
                experiment.getRunType(),
                experiment.getCohortKey(),
                candidate.getBundlePolicyId(),
                actor,
                experiment.getExperimentId());

        candidate.setStatus("PROMOTED");
        candidate.setUpdateTime(LocalDateTime.now());
        candidateMapper.updateById(candidate);

        experiment.setChampionPolicyId(candidate.getBundlePolicyId());
        experiment.setUpdateTime(LocalDateTime.now());
        experimentMapper.updateById(experiment);

        LocalDateTime now = LocalDateTime.now();
        PolicyPromotion promotion = new PolicyPromotion();
        promotion.setExperimentId(experiment.getExperimentId());
        promotion.setCandidateId(candidateId);
        promotion.setRunType(experiment.getRunType());
        promotion.setCohortKey(experiment.getCohortKey());
        promotion.setPreviousPolicyId(previous);
        promotion.setPromotedPolicyId(candidate.getBundlePolicyId());
        promotion.setHardGatesJson(writeJson(gateReport.hardGates()));
        promotion.setMetricDeltasJson(nz(candidate.getGateMetricsJson(), "{}"));
        promotion.setConfidenceJson("{}");
        promotion.setDecision("PROMOTE");
        promotion.setDecidedBy(nz(actor));
        promotion.setDecidedAt(now);
        promotion.setReason(reason);
        promotionMapper.insert(promotion);

        eventService.emit(experiment.getExperimentId(), "PROMOTED", Map.of(
                "candidateId", candidateId,
                "policyId", candidate.getBundlePolicyId(),
                "actor", nz(actor)));
        return toPromotionView(promotion);
    }

    @Transactional
    public PromotionView rollback(String experimentId, String toPolicyId, String reason, String actor) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        requireText(toPolicyId, "toPolicyId");
        PolicyBundleRow target = policyService.getBundle(toPolicyId);
        if (target == null) {
            throw new ApiNotFoundException("policy not found: " + toPolicyId);
        }
        String previous = currentChampionPolicyId(experiment.getRunType(), experiment.getCohortKey());
        policyService.assignChampion(
                experiment.getRunType(),
                experiment.getCohortKey(),
                toPolicyId,
                actor,
                experimentId);

        experiment.setChampionPolicyId(toPolicyId);
        experiment.setUpdateTime(LocalDateTime.now());
        experimentMapper.updateById(experiment);

        LocalDateTime now = LocalDateTime.now();
        PolicyPromotion promotion = new PolicyPromotion();
        promotion.setExperimentId(experimentId);
        promotion.setCandidateId("rollback");
        promotion.setRunType(experiment.getRunType());
        promotion.setCohortKey(experiment.getCohortKey());
        promotion.setPreviousPolicyId(previous);
        promotion.setPromotedPolicyId(toPolicyId);
        promotion.setHardGatesJson("[]");
        promotion.setMetricDeltasJson("{}");
        promotion.setConfidenceJson("{}");
        promotion.setDecision("ROLLBACK");
        promotion.setDecidedBy(nz(actor));
        promotion.setDecidedAt(now);
        promotion.setReason(reason);
        promotionMapper.insert(promotion);

        eventService.emit(experimentId, "ROLLBACK", Map.of(
                "toPolicyId", toPolicyId,
                "previousPolicyId", nz(previous),
                "actor", nz(actor)));
        return toPromotionView(promotion);
    }

    /** Worker: mark experiment RUNNING (idempotent). */
    @Transactional
    public PolicyExperimentView markRunning(String experimentId) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        if (flag(experiment.getCancelRequested())) {
            throw new ApiConflictException("experiment cancel requested");
        }
        if (!"RUNNING".equalsIgnoreCase(experiment.getStatus())) {
            experiment.setStatus("RUNNING");
            experiment.setProgressPhase("RUNNING");
            if (experiment.getStartedAt() == null) {
                experiment.setStartedAt(LocalDateTime.now());
            }
            experiment.setUpdateTime(LocalDateTime.now());
            experimentMapper.updateById(experiment);
            eventService.emit(experimentId, "RUNNING", Map.of());
        }
        return toView(experiment);
    }

    @Transactional
    public PolicyCandidate upsertCandidate(String experimentId, PolicyCandidate draft) {
        requireExperiment(experimentId);
        if (!StringUtils.hasText(draft.getCandidateId())) {
            draft.setCandidateId("pcand-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12));
        }
        draft.setExperimentId(experimentId);
        if (!StringUtils.hasText(draft.getStatus())) {
            draft.setStatus("DRAFT");
        }
        LocalDateTime now = LocalDateTime.now();
        PolicyCandidate existing = candidateMapper.selectById(draft.getCandidateId());
        if (existing == null) {
            draft.setCreateTime(now);
            draft.setUpdateTime(now);
            candidateMapper.insert(draft);
            eventService.emit(experimentId, "CANDIDATE_CREATED", Map.of(
                    "candidateId", draft.getCandidateId(),
                    "status", draft.getStatus()));
        } else {
            draft.setUpdateTime(now);
            candidateMapper.updateById(draft);
            eventService.emit(experimentId, "CANDIDATE_UPDATED", Map.of(
                    "candidateId", draft.getCandidateId(),
                    "status", draft.getStatus()));
        }
        return draft;
    }

    @Transactional
    public PolicyTrial createTrial(PolicyTrial trial) {
        requireExperiment(trial.getExperimentId());
        assertNotCancelled(trial.getExperimentId());
        if (!StringUtils.hasText(trial.getTrialId())) {
            trial.setTrialId("ptrial-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12));
        }
        if (!StringUtils.hasText(trial.getStatus())) {
            trial.setStatus("PENDING");
        }
        trial.setCreateTime(LocalDateTime.now());
        if (trial.getStartedAt() == null && "RUNNING".equalsIgnoreCase(trial.getStatus())) {
            trial.setStartedAt(LocalDateTime.now());
        }
        trialMapper.insert(trial);
        eventService.emit(trial.getExperimentId(), "TRIAL_CREATED", Map.of(
                "trialId", trial.getTrialId(),
                "candidateId", nz(trial.getCandidateId()),
                "caseId", nz(trial.getCaseId()),
                "seed", trial.getSeed() != null ? trial.getSeed() : 0));
        return trial;
    }

    @Transactional
    public PolicyTrial finishTrial(String trialId, PolicyTrial patch) {
        PolicyTrial trial = trialMapper.selectById(trialId);
        if (trial == null) {
            throw new ApiNotFoundException("trial not found: " + trialId);
        }
        if (patch.getStatus() != null) {
            trial.setStatus(patch.getStatus());
        }
        if (patch.getTotalReward() != null) {
            trial.setTotalReward(patch.getTotalReward());
        }
        if (patch.getCostCny() != null) {
            trial.setCostCny(patch.getCostCny());
        }
        if (patch.getLatencyMs() != null) {
            trial.setLatencyMs(patch.getLatencyMs());
        }
        if (patch.getMetricsJson() != null) {
            trial.setMetricsJson(patch.getMetricsJson());
        }
        if (patch.getRewardComponentsJson() != null) {
            trial.setRewardComponentsJson(patch.getRewardComponentsJson());
        }
        if (patch.getError() != null) {
            trial.setError(patch.getError());
        }
        if (patch.getRunId() != null) {
            trial.setRunId(patch.getRunId());
        }
        if (patch.getRunnerSandboxId() != null) {
            trial.setRunnerSandboxId(patch.getRunnerSandboxId());
        }
        if (patch.getEvaluatorSandboxId() != null) {
            trial.setEvaluatorSandboxId(patch.getEvaluatorSandboxId());
        }
        trial.setFinishedAt(LocalDateTime.now());
        trialMapper.updateById(trial);

        PolicyExperiment experiment = requireExperiment(trial.getExperimentId());
        if (trial.getCostCny() != null) {
            BigDecimal spent = experiment.getSpentCny() != null ? experiment.getSpentCny() : BigDecimal.ZERO;
            experiment.setSpentCny(spent.add(trial.getCostCny()));
            experiment.setUpdateTime(LocalDateTime.now());
            experimentMapper.updateById(experiment);
        }
        eventService.emit(trial.getExperimentId(), "TRIAL_FINISHED", Map.of(
                "trialId", trialId,
                "status", nz(trial.getStatus())));
        return trial;
    }

    @Transactional
    public void recordGate(String experimentId, String candidateId, String gateMetricsJson, boolean passed) {
        PolicyCandidate candidate = candidateMapper.selectById(candidateId);
        if (candidate == null || !experimentId.equals(candidate.getExperimentId())) {
            throw new ApiNotFoundException("candidate not found in experiment");
        }
        candidate.setGateMetricsJson(gateMetricsJson);
        candidate.setStatus(passed ? "PASSED_GATE" : "REJECTED");
        candidate.setUpdateTime(LocalDateTime.now());
        candidateMapper.updateById(candidate);
        // NEVER promote here.
        eventService.emit(experimentId, "GATE_RECORDED", Map.of(
                "candidateId", candidateId,
                "passed", passed,
                "autoPromote", false));
    }

    public boolean isPauseOrCancelRequested(String experimentId) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        return flag(experiment.getPauseRequested()) || flag(experiment.getCancelRequested());
    }

    private PolicyExperiment requireExperiment(String experimentId) {
        PolicyExperiment experiment = experimentMapper.selectById(experimentId);
        if (experiment == null) {
            throw new ApiNotFoundException("experiment not found: " + experimentId);
        }
        return experiment;
    }

    private void assertMutable(PolicyExperiment experiment) {
        String status = experiment.getStatus();
        if ("COMPLETED".equalsIgnoreCase(status)
                || "FAILED".equalsIgnoreCase(status)
                || "CANCELLED".equalsIgnoreCase(status)) {
            throw new ApiConflictException("experiment is terminal: " + status);
        }
    }

    private void assertNotCancelled(String experimentId) {
        PolicyExperiment experiment = requireExperiment(experimentId);
        if (flag(experiment.getCancelRequested()) || "CANCELLED".equalsIgnoreCase(experiment.getStatus())) {
            throw new ApiConflictException("experiment cancelled");
        }
        if (flag(experiment.getPauseRequested()) || "PAUSED".equalsIgnoreCase(experiment.getStatus())) {
            throw new ApiConflictException("experiment paused");
        }
    }

    private String currentChampionPolicyId(String runType, String cohortKey) {
        String cohort = StringUtils.hasText(cohortKey) ? cohortKey : "default";
        try {
            PolicyChampionAssignmentRow row = championAssignmentMapper.selectOne(
                    new QueryWrapper<PolicyChampionAssignmentRow>()
                            .eq("run_type", runType)
                            .eq("cohort_key", cohort)
                            .eq("active", 1)
                            .last("limit 1"));
            if (row != null && StringUtils.hasText(row.getPolicyId())) {
                return row.getPolicyId();
            }
        } catch (Exception ignored) {
            // table may be empty pre-migration
        }
        return policyService.listActiveBundles().stream()
                .filter(b -> b.getIsChampion() != null && b.getIsChampion() == 1)
                .map(PolicyBundleRow::getPolicyId)
                .findFirst()
                .orElse(null);
    }

    private List<String> actionsAllowed(PolicyExperiment experiment, List<PolicyCandidate> candidates) {
        List<String> actions = new ArrayList<>();
        String status = experiment.getStatus();
        if ("PENDING".equalsIgnoreCase(status) || "RUNNING".equalsIgnoreCase(status)) {
            actions.add("pause");
            actions.add("cancel");
        }
        if ("PAUSED".equalsIgnoreCase(status)) {
            actions.add("resume");
            actions.add("cancel");
        }
        if ("COMPLETED".equalsIgnoreCase(status)
                || "FAILED".equalsIgnoreCase(status)
                || "CANCELLED".equalsIgnoreCase(status)
                || "PAUSED".equalsIgnoreCase(status)) {
            actions.add("rerun");
        }
        actions.add("rollback");
        boolean canPromote = candidates.stream()
                .anyMatch(c -> "PASSED_GATE".equalsIgnoreCase(c.getStatus()));
        if (canPromote) {
            actions.add("promote");
        }
        return actions;
    }

    private PolicyExperimentView toView(PolicyExperiment row) {
        return new PolicyExperimentView(
                row.getExperimentId(),
                row.getKind(),
                row.getStatus(),
                row.getGeneration(),
                row.getChampionPolicyId(),
                row.getBasePolicyId(),
                row.getRunType(),
                row.getCohortKey(),
                row.getEvalDataset(),
                row.getGateDataset(),
                row.getSafetyDataset(),
                readSeeds(row.getSeedsJson()),
                row.getRepeatsPerCase(),
                row.getCaseLimit(),
                row.getBudgetCny(),
                row.getSpentCny(),
                row.getProgressPct(),
                row.getProgressPhase(),
                flag(row.getPauseRequested()),
                flag(row.getCancelRequested()),
                flag(row.getAutoPromote()),
                row.getNote(),
                row.getError(),
                row.getCreatedBy(),
                row.getStartedAt(),
                row.getFinishedAt(),
                row.getCreateTime());
    }

    private PolicyCandidateView toCandidateView(PolicyCandidate row) {
        return new PolicyCandidateView(
                row.getCandidateId(),
                row.getExperimentId(),
                row.getParentPolicyId(),
                row.getBundlePolicyId(),
                row.getConfigHash(),
                row.getMutationReason(),
                row.getStatus(),
                row.getGateMetricsJson(),
                row.getCreateTime());
    }

    private PolicyTrialView toTrialView(PolicyTrial row) {
        return new PolicyTrialView(
                row.getTrialId(),
                row.getExperimentId(),
                row.getCandidateId(),
                row.getDatasetSplit(),
                row.getCaseId(),
                row.getRepeatNo(),
                row.getSeed(),
                row.getRunId(),
                row.getStatus(),
                row.getTotalReward(),
                row.getCostCny(),
                row.getLatencyMs(),
                row.getRunnerSandboxId(),
                row.getEvaluatorSandboxId(),
                row.getError(),
                row.getStartedAt(),
                row.getFinishedAt());
    }

    private SandboxDiagnosticView toSandboxView(SandboxExecutionRow row) {
        String purpose = StringUtils.hasText(row.getPurpose()) ? row.getPurpose() : "UNKNOWN";
        String isolation = row.getContainerId() != null && !row.getContainerId().isBlank()
                ? "docker_isolated" : "unknown";
        return new SandboxDiagnosticView(
                row.getSandboxId(),
                purpose,
                row.getExperimentId(),
                row.getTrialId(),
                row.getToolName(),
                row.getStatus(),
                row.getExitCode(),
                row.getDurationMs(),
                row.getError(),
                isolation,
                row.getCreateTime());
    }

    private PromotionView toPromotionView(PolicyPromotion row) {
        return new PromotionView(
                row.getId(),
                row.getExperimentId(),
                row.getCandidateId(),
                row.getRunType(),
                row.getCohortKey(),
                row.getPreviousPolicyId(),
                row.getPromotedPolicyId(),
                row.getDecision(),
                row.getDecidedBy(),
                row.getDecidedAt(),
                row.getReason());
    }

    private List<Long> readSeeds(String json) {
        if (!StringUtils.hasText(json)) {
            return List.of();
        }
        try {
            return objectMapper.readValue(json, new TypeReference<>() {
            });
        } catch (Exception e) {
            return List.of();
        }
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "[]";
        }
    }

    private static void requireText(String value, String field) {
        if (!StringUtils.hasText(value)) {
            throw new IllegalArgumentException(field + " required");
        }
    }

    private static boolean flag(Integer value) {
        return value != null && value != 0;
    }

    private static String nz(String value) {
        return value != null ? value : "";
    }

    private static String nz(String value, String fallback) {
        return StringUtils.hasText(value) ? value : fallback;
    }
}
