package com.resumai.agent.service.policylab;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.ApiConflictException;
import com.resumai.agent.api.dto.policylab.CreatePolicyExperimentRequest;
import com.resumai.agent.api.dto.policylab.PolicyExperimentView;
import com.resumai.agent.dao.PolicyCandidateMapper;
import com.resumai.agent.dao.PolicyChampionAssignmentMapper;
import com.resumai.agent.dao.PolicyExperimentMapper;
import com.resumai.agent.dao.PolicyPromotionMapper;
import com.resumai.agent.dao.PolicyTrialMapper;
import com.resumai.agent.dao.SandboxExecutionMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicyCandidate;
import com.resumai.agent.domain.entity.PolicyExperiment;
import com.resumai.agent.service.run.PolicyService;
import java.math.BigDecimal;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

class PolicyLabServiceTest {

    private PolicyExperimentMapper experimentMapper;
    private PolicyCandidateMapper candidateMapper;
    private PolicyTrialMapper trialMapper;
    private PolicyPromotionMapper promotionMapper;
    private PolicyChampionAssignmentMapper championAssignmentMapper;
    private SandboxExecutionMapper sandboxExecutionMapper;
    private PolicyService policyService;
    private PolicyLabEventService eventService;
    private PolicyLabService service;

    @BeforeEach
    void setUp() {
        experimentMapper = mock(PolicyExperimentMapper.class);
        candidateMapper = mock(PolicyCandidateMapper.class);
        trialMapper = mock(PolicyTrialMapper.class);
        promotionMapper = mock(PolicyPromotionMapper.class);
        championAssignmentMapper = mock(PolicyChampionAssignmentMapper.class);
        sandboxExecutionMapper = mock(SandboxExecutionMapper.class);
        policyService = mock(PolicyService.class);
        eventService = mock(PolicyLabEventService.class);
        when(eventService.emit(anyString(), anyString(), anyMap())).thenReturn(null);
        service = new PolicyLabService(
                experimentMapper, candidateMapper, trialMapper, promotionMapper,
                championAssignmentMapper, sandboxExecutionMapper, policyService,
                eventService, new PolicyLabEvaluator(), new ObjectMapper());
    }

    @Test
    void createForcesAutoPromoteFalseEvenWhenRequestedTrue() {
        PolicyBundleRow base = new PolicyBundleRow();
        base.setPolicyId("balanced");
        when(policyService.getBundle("balanced")).thenReturn(base);
        doAnswer(inv -> {
            PolicyExperiment row = inv.getArgument(0);
            assertEquals(0, row.getAutoPromote());
            return 1;
        }).when(experimentMapper).insert(any(PolicyExperiment.class));

        CreatePolicyExperimentRequest request = new CreatePolicyExperimentRequest(
                "OFFLINE_SEARCH",
                "balanced",
                "full_evaluation",
                "default",
                "gold",
                "regression",
                "safety",
                List.of(42L),
                1,
                1,
                new BigDecimal("0.5"),
                "test",
                true);

        PolicyExperimentView view = service.create(request, "tester");
        assertFalse(view.autoPromote());
        ArgumentCaptor<PolicyExperiment> captor = ArgumentCaptor.forClass(PolicyExperiment.class);
        verify(experimentMapper).insert(captor.capture());
        assertEquals(0, captor.getValue().getAutoPromote());
    }

    @Test
    void promoteRejectsNonPassedGate() {
        PolicyCandidate candidate = new PolicyCandidate();
        candidate.setCandidateId("c1");
        candidate.setExperimentId("e1");
        candidate.setStatus("EVALUATING");
        candidate.setBundlePolicyId("mut-1");
        when(candidateMapper.selectById("c1")).thenReturn(candidate);

        ApiConflictException ex = assertThrows(ApiConflictException.class,
                () -> service.promote("c1", "nope", "tester"));
        assertTrue(ex.getMessage().contains("has not passed hard gates"));
        verify(policyService, never()).assignChampion(anyString(), anyString(), anyString(),
                anyString(), anyString());
        verify(promotionMapper, never()).insert(any(com.resumai.agent.domain.entity.PolicyPromotion.class));
    }

    @Test
    void promoteAcceptsPassedGate() {
        PolicyCandidate candidate = new PolicyCandidate();
        candidate.setCandidateId("c1");
        candidate.setExperimentId("e1");
        candidate.setStatus("PASSED_GATE");
        candidate.setBundlePolicyId("mut-1");
        candidate.setGateMetricsJson("{\"passed\":true}");
        when(candidateMapper.selectById("c1")).thenReturn(candidate);

        PolicyExperiment experiment = new PolicyExperiment();
        experiment.setExperimentId("e1");
        experiment.setRunType("full_evaluation");
        experiment.setCohortKey("default");
        when(experimentMapper.selectById("e1")).thenReturn(experiment);
        when(trialMapper.selectList(any(QueryWrapper.class))).thenReturn(List.of());
        when(championAssignmentMapper.selectOne(any(QueryWrapper.class))).thenReturn(null);
        when(policyService.listActiveBundles()).thenReturn(List.of());

        service.promote("c1", "manual promote", "tester");

        verify(policyService).assignChampion(
                eq("full_evaluation"), eq("default"), eq("mut-1"), eq("tester"), eq("e1"));
        verify(promotionMapper).insert(any(com.resumai.agent.domain.entity.PolicyPromotion.class));
        assertEquals("PROMOTED", candidate.getStatus());
    }
}
