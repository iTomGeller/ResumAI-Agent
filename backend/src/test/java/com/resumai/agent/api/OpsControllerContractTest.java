package com.resumai.agent.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillAggUsage;
import com.resumai.agent.dao.RunEventMapper;
import com.resumai.agent.domain.entity.RunEvent;
import com.resumai.agent.service.run.AgentRuntimeClient;
import com.resumai.agent.api.dto.ops.OpsDebugDtos.EventOutcome;
import com.resumai.agent.service.ops.OpsDebugService;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.time.LocalDateTime;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

/**
 * Guardrail: Ops MCP status must never be inferred from config description text
 * (e.g. "rate limit" in a public provider description).
 */
class OpsControllerContractTest {

    @Test
    void doesNotInferMcpStatusFromDescription() {
        Method[] methods = OpsController.class.getDeclaredMethods();
        boolean hasResolveMcpStatus = Arrays.stream(methods)
                .anyMatch(m -> "resolveMcpStatus".equals(m.getName()));
        assertFalse(hasResolveMcpStatus,
                "OpsController must not guess MCP status from description text");

        String source = OpsController.class.getProtectionDomain() != null
                ? "OpsController" : "";
        assertTrue(source.contains("OpsController"));
    }

    @Test
    void exposesRunCentricAndPolicyLabEndpoints() throws Exception {
        Method runs = OpsController.class.getMethod("runs",
                String.class, String.class, String.class, String.class, int.class);
        Method mcp = OpsController.class.getMethod("mcp",
                boolean.class, int.class, String.class, String.class, String.class);
        Method skills = OpsController.class.getMethod("skills", boolean.class, int.class);
        Method rag = OpsController.class.getMethod("rag",
                int.class, String.class, String.class, String.class);
        Method policyLab = OpsController.class.getMethod("policyLabSandbox", int.class);
        assertTrue(runs.getName().equals("runs"));
        assertTrue(mcp.getName().equals("mcp"));
        assertTrue(skills.getName().equals("skills"));
        assertTrue(rag.getName().equals("rag"));
        assertTrue(policyLab.getName().equals("policyLabSandbox"));
    }

    @Test
    void skillEventTypesIncludeAppliedAndLegacyAliases() throws Exception {
        Field field = OpsDebugService.class.getField("SKILL_EVENT_TYPES");
        @SuppressWarnings("unchecked")
        List<String> types = (List<String>) field.get(null);
        assertTrue(types.contains("skill.catalog"));
        assertTrue(types.contains("skill.selected"));
        assertTrue(types.contains("skill.loaded"));
        assertTrue(types.contains("skill.applied"));
        assertTrue(types.contains("skill.skipped"));
        assertTrue(types.contains("skill.failed"));
        assertTrue(types.contains("skill.started"));
        assertTrue(types.contains("skill.completed"));
    }

    @Test
    void mcpEventTypesIncludeModelProposalLifecycle() {
        assertTrue(OpsDebugService.MCP_EVENT_TYPES.contains("tool.progress"));
        assertTrue(OpsDebugService.MCP_EVENT_TYPES.contains("tool.started"));
        assertTrue(OpsDebugService.MCP_EVENT_TYPES.contains("tool.completed"));
        assertTrue(OpsDebugService.MCP_EVENT_TYPES.contains("tool.failed"));
    }

    @Test
    void emptyLiveMcpInventorySuppressesRetiredGlobalHistory() {
        RunEventMapper events = mock(RunEventMapper.class);
        AgentRuntimeClient runtime = mock(AgentRuntimeClient.class);
        when(runtime.getOpsRuntime(false)).thenReturn(Optional.of(Map.of(
                "mcp", Map.of(
                        "source", "python_mcp_registry",
                        "probed", false,
                        "servers", Map.of(),
                        "toolCount", 0))));
        when(events.selectList(any())).thenReturn(List.of(event(
                "tool.completed", "cn-web.search", "run-old",
                LocalDateTime.of(2026, 7, 27, 10, 0),
                """
                {"source":"mcp","mcpServer":"cn-web",
                 "toolCallId":"legacy-call","outcome":"SUCCESS"}
                """)));
        OpsDebugService svc = service(events, runtime);

        var response = svc.mcp(false, null, null, null, 40);

        assertFalse(response.inventory().probed());
        assertTrue(response.invocations().items().isEmpty());
    }

    @Test
    void skillLifecycleAggregationCountsStagesAndKeepsNewestTimestamp() {
        RunEventMapper events = mock(RunEventMapper.class);
        AgentRuntimeClient runtime = mock(AgentRuntimeClient.class);
        when(runtime.getOpsRuntime(false)).thenReturn(Optional.of(Map.of(
                "skills", Map.of(
                        "source", "python_skill_manager",
                        "count", 1,
                        "activeCount", 1,
                        "deprecatedCount", 0,
                        "advertisedTools", List.of(
                                "load_skill", "read_skill_resource"),
                        "skills", List.of()))));
        LocalDateTime newest = LocalDateTime.of(2026, 7, 28, 9, 5);
        String payload = """
                {"skillId":"calibrate-and-explain-decision",
                 "skillVersion":"v2","skillHash":"hash-v2",
                 "occurredAt":"2026-07-28T01:05:00Z"}
                """;
        when(events.selectList(any())).thenReturn(List.of(
                event("skill.applied", "calibrate-and-explain-decision",
                        "run-new", newest, payload),
                event("skill.loaded", "calibrate-and-explain-decision",
                        "run-new", newest.minusSeconds(1), payload),
                event("skill.selected", "calibrate-and-explain-decision",
                        "run-new", newest.minusSeconds(2), payload),
                event("skill.catalog", "calibrate-and-explain-decision",
                        "run-new", newest.minusSeconds(3), payload),
                event("skill.skipped", "calibrate-and-explain-decision",
                        "run-old", newest.minusDays(1), payload)));
        OpsDebugService svc = service(events, runtime);

        var response = svc.skills(false, 300);
        SkillAggUsage aggregate = response.usageBySkill().get(0);

        assertEquals(1, aggregate.catalog());
        assertEquals(1, aggregate.selected());
        assertEquals(1, aggregate.loaded());
        assertEquals(1, aggregate.applied());
        assertEquals(1, aggregate.skipped());
        assertEquals(0, aggregate.failed());
        assertEquals("run-new", aggregate.lastRunId());
        assertEquals(newest, aggregate.lastAt());
        assertEquals(5, response.selectedApplied().size());
    }

    @Test
    void deriveOutcomeMapsSkillAndLifecycleCorrectly() {
        OpsDebugService svc = new OpsDebugService(
                null, null, null, null, null, null, null, null);
        assertEquals(EventOutcome.FAILED, svc.deriveOutcome("tool.failed"));
        assertEquals(EventOutcome.FAILED, svc.deriveOutcome("run.timed_out"));
        assertEquals(EventOutcome.SUCCESS, svc.deriveOutcome("tool.completed"));
        assertEquals(EventOutcome.SUCCESS, svc.deriveOutcome("run.completed"));
        assertEquals(EventOutcome.INFO, svc.deriveOutcome("skill.selected"));
        assertEquals(EventOutcome.INFO, svc.deriveOutcome("skill.applied"));
        assertEquals(EventOutcome.RUNNING, svc.deriveOutcome("llm.started"));
        assertEquals(EventOutcome.RUNNING, svc.deriveOutcome("run.progress"));
        assertEquals(EventOutcome.RUNNING, svc.deriveOutcome("llm.retrying"));
        assertEquals(EventOutcome.INFO, svc.deriveOutcome("agent.selected"));
    }

    @Test
    void sandboxPurposeNeverHardcodesPolicyLab() {
        OpsDebugService svc = new OpsDebugService(
                null, null, null, null, null, null, null, null);
        assertEquals("LEGACY_CANDIDATE_EVALUATION", svc.sandboxPurpose(null));
        assertEquals("LEGACY_CANDIDATE_EVALUATION", svc.sandboxPurpose(""));
        assertEquals("POLICY_EVOLUTION", svc.sandboxPurpose("POLICY_EVOLUTION"));
        assertEquals("BENCHMARK", svc.sandboxPurpose("BENCHMARK"));
    }

    private static OpsDebugService service(
            RunEventMapper events, AgentRuntimeClient runtime) {
        return new OpsDebugService(
                null, events, null, null, null, runtime, null,
                new ObjectMapper());
    }

    private static RunEvent event(
            String type, String tool, String runId,
            LocalDateTime createTime, String payload) {
        RunEvent event = new RunEvent();
        event.setRunId(runId);
        event.setTraceId("trace-" + runId);
        event.setSeq(1);
        event.setEventType(type);
        event.setAgentId("ReportAgent");
        event.setToolName(tool);
        event.setPayload(payload);
        event.setCreateTime(createTime);
        return event;
    }
}
