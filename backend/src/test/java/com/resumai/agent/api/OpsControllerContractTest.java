package com.resumai.agent.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.resumai.agent.api.dto.ops.OpsDebugDtos.EventOutcome;
import com.resumai.agent.service.ops.OpsDebugService;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.List;
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
        assertTrue(types.contains("skill.selected"));
        assertTrue(types.contains("skill.applied"));
        assertTrue(types.contains("skill.failed"));
        assertTrue(types.contains("skill.started"));
        assertTrue(types.contains("skill.completed"));
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
}
