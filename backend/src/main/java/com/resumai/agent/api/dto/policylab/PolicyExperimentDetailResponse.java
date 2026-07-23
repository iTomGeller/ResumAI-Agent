package com.resumai.agent.api.dto.policylab;

import java.util.List;

public record PolicyExperimentDetailResponse(
        PolicyExperimentView experiment,
        List<PolicyCandidateView> candidates,
        List<PolicyTrialView> trials,
        List<HardGateView> hardGates,
        List<SandboxDiagnosticView> sandboxes,
        List<String> actionsAllowed
) {
}
