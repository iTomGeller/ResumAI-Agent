package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class MemoryRedactionTest {

    @Test
    void redactsApiKeysAndPasswords() {
        String content = "调用配置 api_key=sk-abcdef1234567890abcdef 密码 password: hunter2secret "
                + "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload";
        String redacted = MemoryService.redactSecrets(content);
        assertFalse(redacted.contains("sk-abcdef1234567890abcdef"));
        assertFalse(redacted.contains("hunter2secret"));
        assertFalse(redacted.contains("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"));
        assertTrue(redacted.contains("[REDACTED]"));
    }

    @Test
    void keepsNormalTechnicalContent() {
        String content = "候选人使用 Redis 作为缓存，Kafka 峰值 5000 QPS";
        assertEquals(content, MemoryService.redactSecrets(content));
    }

    @Test
    void detectsBenchmarkSources() {
        assertTrue(MemoryService.isBenchmarkSource("exp5_benchmark"));
        assertTrue(MemoryService.isBenchmarkSource("exp_benchmark"));
        assertTrue(MemoryService.isBenchmarkSource("exp12_benchmark"));
        assertFalse(MemoryService.isBenchmarkSource("system_rule"));
        assertFalse(MemoryService.isBenchmarkSource("user_explicit"));
        assertFalse(MemoryService.isBenchmarkSource("experiment_lab"));
    }

    @Test
    void controlPlaneErrorCodesIsolated() {
        assertTrue(MemoryService.isControlPlaneErrorCode("ORPHANED_ON_RESTART"));
        assertTrue(MemoryService.isControlPlaneErrorCode("RUNTIME_START_FAILED"));
        assertTrue(MemoryService.isControlPlaneErrorCode("START_STUCK"));
        assertFalse(MemoryService.isControlPlaneErrorCode("BUDGET_EXCEEDED"));
    }

    @Test
    void failureOnlyForCoordinatorAndPolicy() {
        assertTrue(MemoryService.allowsFailure("CoordinatorAgent"));
        assertTrue(MemoryService.allowsFailure("PolicyEvolution"));
        assertTrue(MemoryService.allowsFailure("POLICY_EVOLUTION"));
        assertFalse(MemoryService.allowsFailure("ReportAgent"));
        assertFalse(MemoryService.allowsFailure("RiskAgent"));
        assertFalse(MemoryService.allowsFailure("TechAgent"));
        assertFalse(MemoryService.allowsFailure(null));
        assertFalse(MemoryService.allowsFailure("SpecialistAgent"));
    }

    @Test
    void reportAndRiskIdentified() {
        assertTrue(MemoryService.isReportOrRisk("ReportAgent"));
        assertTrue(MemoryService.isReportOrRisk("RiskAgent"));
        assertFalse(MemoryService.isReportOrRisk("CoordinatorAgent"));
    }
}
