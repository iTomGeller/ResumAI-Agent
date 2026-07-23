package com.resumai.agent.service.policylab;

import com.resumai.agent.api.dto.policylab.HardGateView;
import com.resumai.agent.domain.entity.PolicyCandidate;
import com.resumai.agent.domain.entity.PolicyExperiment;
import com.resumai.agent.domain.entity.PolicyTrial;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Component;

/**
 * Stub evaluator — parses stored gate_metrics_json / trial metrics into hard-gate views.
 * Full paired-seed bootstrap CI lives in the Python worker.
 */
@Component
public class PolicyLabEvaluator {

    public record GateReport(boolean allPassed, List<HardGateView> hardGates) {
    }

    public GateReport evaluateStored(PolicyExperiment experiment,
                                     PolicyCandidate candidate,
                                     List<PolicyTrial> trials) {
        List<HardGateView> gates = new ArrayList<>();
        String metrics = candidate != null ? candidate.getGateMetricsJson() : null;
        if (metrics != null && metrics.contains("\"passed\":false")) {
            gates.add(new HardGateView("stored_gate_report", "FAILED", "gate_metrics_json.passed=false"));
        } else if (metrics != null && metrics.contains("\"passed\":true")) {
            gates.add(new HardGateView("stored_gate_report", "PASSED", "gate_metrics_json.passed=true"));
        } else if ("PASSED_GATE".equalsIgnoreCase(candidate != null ? candidate.getStatus() : null)) {
            gates.add(new HardGateView("candidate_status", "PASSED", "status=PASSED_GATE"));
        } else {
            gates.add(new HardGateView("candidate_status", "PENDING",
                    "candidate status=" + (candidate != null ? candidate.getStatus() : "null")));
        }

        long safetyFailures = trials == null ? 0 : trials.stream()
                .filter(t -> "safety".equalsIgnoreCase(t.getDatasetSplit()))
                .filter(t -> t.getStatus() != null && !"SUCCEEDED".equalsIgnoreCase(t.getStatus()))
                .count();
        gates.add(new HardGateView(
                "safety_zero_violations",
                safetyFailures == 0 ? "PASSED" : "FAILED",
                "failedSafetyTrials=" + safetyFailures));

        boolean allPassed = gates.stream().allMatch(g -> "PASSED".equalsIgnoreCase(g.status()));
        return new GateReport(allPassed, gates);
    }
}
