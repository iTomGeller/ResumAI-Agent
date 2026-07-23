package com.resumai.agent.api.dev;

import com.resumai.agent.api.dto.policylab.CreatePolicyExperimentRequest;
import com.resumai.agent.api.dto.policylab.PolicyCandidateView;
import com.resumai.agent.api.dto.policylab.PolicyExperimentDetailResponse;
import com.resumai.agent.api.dto.policylab.PolicyExperimentEventView;
import com.resumai.agent.api.dto.policylab.PolicyExperimentView;
import com.resumai.agent.api.dto.policylab.PolicyTrialView;
import com.resumai.agent.api.dto.policylab.PromotePolicyCandidateRequest;
import com.resumai.agent.api.dto.policylab.PromotionView;
import com.resumai.agent.api.dto.policylab.RollbackPolicyRequest;
import com.resumai.agent.domain.entity.PolicyCandidate;
import com.resumai.agent.domain.entity.PolicyTrial;
import com.resumai.agent.service.policylab.PolicyLabEventService;
import com.resumai.agent.service.policylab.PolicyLabService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import java.io.IOException;
import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.springframework.http.MediaType;
import org.springframework.util.StringUtils;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * Developer Policy Lab control plane — create / pause / promote / rollback.
 * Promotion is always manual (autoPromote forced false on create).
 */
@RestController
@RequestMapping("/api/dev/policy-lab")
public class PolicyLabController {

    private final PolicyLabService policyLabService;
    private final PolicyLabEventService eventService;

    public PolicyLabController(PolicyLabService policyLabService,
                               PolicyLabEventService eventService) {
        this.policyLabService = policyLabService;
        this.eventService = eventService;
    }

    @PostMapping("/experiments")
    public PolicyExperimentView create(@Valid @RequestBody CreatePolicyExperimentRequest request,
                                       HttpServletRequest http) {
        return policyLabService.create(request, actor(http));
    }

    @GetMapping("/experiments")
    public List<PolicyExperimentView> list(@RequestParam(defaultValue = "50") int limit) {
        return policyLabService.list(limit);
    }

    @GetMapping("/experiments/{id}")
    public PolicyExperimentDetailResponse get(@PathVariable("id") String id) {
        return policyLabService.getDetail(id);
    }

    @PostMapping("/experiments/{id}/pause")
    public PolicyExperimentView pause(@PathVariable("id") String id, HttpServletRequest http) {
        return policyLabService.requestPause(id, actor(http));
    }

    @PostMapping("/experiments/{id}/resume")
    public PolicyExperimentView resume(@PathVariable("id") String id, HttpServletRequest http) {
        return policyLabService.resume(id, actor(http));
    }

    @PostMapping("/experiments/{id}/cancel")
    public PolicyExperimentView cancel(@PathVariable("id") String id, HttpServletRequest http) {
        return policyLabService.requestCancel(id, actor(http));
    }

    @PostMapping("/experiments/{id}/rerun")
    public PolicyExperimentView rerun(@PathVariable("id") String id, HttpServletRequest http) {
        return policyLabService.cloneForRerun(id, actor(http));
    }

    @PostMapping("/candidates/{id}/promote")
    public PromotionView promote(@PathVariable("id") String id,
                                 @RequestBody(required = false) PromotePolicyCandidateRequest request,
                                 HttpServletRequest http) {
        String reason = request != null ? request.reason() : null;
        return policyLabService.promote(id, reason, actor(http));
    }

    @PostMapping("/experiments/{id}/rollback")
    public PromotionView rollback(@PathVariable("id") String id,
                                  @Valid @RequestBody RollbackPolicyRequest request,
                                  HttpServletRequest http) {
        return policyLabService.rollback(id, request.toPolicyId(), request.reason(), actor(http));
    }

    /** Polling fallback for SSE clients. */
    @GetMapping("/experiments/{id}/events")
    public List<PolicyExperimentEventView> events(@PathVariable("id") String id,
                                                  @RequestParam(defaultValue = "0") int afterSeq,
                                                  @RequestParam(defaultValue = "100") int limit) {
        return eventService.listAfter(id, afterSeq, limit);
    }

    /** SSE stream (also available at /sse/policy-lab/{id}). */
    @GetMapping(value = "/sse/experiments/{id}", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter sse(@PathVariable("id") String id,
                          @RequestParam(defaultValue = "0") int afterSeq) {
        return openSse(id, afterSeq);
    }

    // ---- Worker write APIs (policy-lab-worker / PolicyExperimentRunner) ----

    @PostMapping("/experiments/{id}/start")
    public PolicyExperimentView start(@PathVariable("id") String id) {
        return policyLabService.markRunning(id);
    }

    @PostMapping("/experiments/{id}/candidates")
    public PolicyCandidateView upsertCandidate(@PathVariable("id") String id,
                                               @RequestBody Map<String, Object> body) {
        PolicyCandidate draft = new PolicyCandidate();
        draft.setCandidateId(str(body.get("candidateId")));
        draft.setParentPolicyId(str(body.get("parentPolicyId")));
        draft.setBundlePolicyId(str(body.get("bundlePolicyId")));
        draft.setConfigJson(str(body.get("configJson")));
        draft.setConfigHash(str(body.get("configHash")));
        draft.setMutationPatch(str(body.get("mutationPatch")));
        draft.setMutationReason(str(body.get("mutationReason")));
        draft.setReflectorModel(str(body.get("reflectorModel")));
        draft.setStatus(str(body.get("status")));
        draft.setGateMetricsJson(str(body.get("gateMetricsJson")));
        PolicyCandidate saved = policyLabService.upsertCandidate(id, draft);
        return new PolicyCandidateView(
                saved.getCandidateId(), saved.getExperimentId(), saved.getParentPolicyId(),
                saved.getBundlePolicyId(), saved.getConfigHash(), saved.getMutationReason(),
                saved.getStatus(), saved.getGateMetricsJson(), saved.getCreateTime());
    }

    @PostMapping("/experiments/{id}/trials")
    public PolicyTrialView createTrial(@PathVariable("id") String id,
                                       @RequestBody Map<String, Object> body) {
        PolicyTrial trial = new PolicyTrial();
        trial.setExperimentId(id);
        trial.setTrialId(str(body.get("trialId")));
        trial.setCandidateId(str(body.get("candidateId")));
        trial.setDatasetSplit(str(body.getOrDefault("datasetSplit", "eval")));
        trial.setCaseId(str(body.get("caseId")));
        trial.setRepeatNo(intOr(body.get("repeatNo"), 1));
        trial.setSeed(longOr(body.get("seed")));
        trial.setRunId(str(body.get("runId")));
        trial.setStatus(str(body.getOrDefault("status", "PENDING")));
        PolicyTrial saved = policyLabService.createTrial(trial);
        return toTrialView(saved);
    }

    @PostMapping("/trials/{trialId}/finish")
    public PolicyTrialView finishTrial(@PathVariable String trialId,
                                       @RequestBody Map<String, Object> body) {
        PolicyTrial patch = new PolicyTrial();
        patch.setStatus(str(body.get("status")));
        if (body.get("totalReward") != null) {
            patch.setTotalReward(new BigDecimal(String.valueOf(body.get("totalReward"))));
        }
        if (body.get("costCny") != null) {
            patch.setCostCny(new BigDecimal(String.valueOf(body.get("costCny"))));
        }
        if (body.get("latencyMs") != null) {
            patch.setLatencyMs(intOr(body.get("latencyMs"), null));
        }
        patch.setMetricsJson(str(body.get("metricsJson")));
        patch.setRewardComponentsJson(str(body.get("rewardComponentsJson")));
        patch.setError(str(body.get("error")));
        patch.setRunId(str(body.get("runId")));
        patch.setRunnerSandboxId(str(body.get("runnerSandboxId")));
        patch.setEvaluatorSandboxId(str(body.get("evaluatorSandboxId")));
        return toTrialView(policyLabService.finishTrial(trialId, patch));
    }

    @PostMapping("/experiments/{id}/gate")
    public Map<String, Object> recordGate(@PathVariable("id") String id,
                                          @RequestBody Map<String, Object> body) {
        String candidateId = str(body.get("candidateId"));
        boolean passed = Boolean.TRUE.equals(body.get("passed"))
                || "true".equalsIgnoreCase(String.valueOf(body.get("passed")));
        String metrics = body.get("gateMetricsJson") != null
                ? String.valueOf(body.get("gateMetricsJson"))
                : String.valueOf(body.getOrDefault("metrics", "{}"));
        policyLabService.recordGate(id, candidateId, metrics, passed);
        return Map.of("ok", true, "passed", passed, "promoted", false);
    }

    private SseEmitter openSse(String experimentId, int afterSeq) {
        SseEmitter emitter = new SseEmitter(TimeUnit.MINUTES.toMillis(30));
        Thread worker = new Thread(() -> {
            int cursor = afterSeq;
            try {
                for (int i = 0; i < 600; i++) {
                    List<PolicyExperimentEventView> batch = eventService.listAfter(experimentId, cursor, 50);
                    for (PolicyExperimentEventView event : batch) {
                        emitter.send(SseEmitter.event()
                                .name(event.eventType())
                                .id(String.valueOf(event.seq()))
                                .data(event));
                        cursor = event.seq();
                    }
                    Thread.sleep(1000L);
                }
                emitter.complete();
            } catch (IOException | InterruptedException ex) {
                emitter.completeWithError(ex);
                Thread.currentThread().interrupt();
            }
        }, "policy-lab-sse-" + experimentId);
        worker.setDaemon(true);
        worker.start();
        return emitter;
    }

    private static String actor(HttpServletRequest http) {
        String header = http.getHeader("X-Developer-Actor");
        if (StringUtils.hasText(header)) {
            return header.trim();
        }
        return "developer";
    }

    private static String str(Object value) {
        if (value == null) {
            return null;
        }
        String text = String.valueOf(value);
        return text.isBlank() || "null".equals(text) ? null : text;
    }

    private static Integer intOr(Object value, Integer fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Integer.parseInt(String.valueOf(value));
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    private static Long longOr(Object value) {
        if (value == null) {
            return null;
        }
        try {
            return Long.parseLong(String.valueOf(value));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static PolicyTrialView toTrialView(PolicyTrial row) {
        return new PolicyTrialView(
                row.getTrialId(), row.getExperimentId(), row.getCandidateId(),
                row.getDatasetSplit(), row.getCaseId(), row.getRepeatNo(), row.getSeed(),
                row.getRunId(), row.getStatus(), row.getTotalReward(), row.getCostCny(),
                row.getLatencyMs(), row.getRunnerSandboxId(), row.getEvaluatorSandboxId(),
                row.getError(), row.getStartedAt(), row.getFinishedAt());
    }
}
