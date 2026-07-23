package com.resumai.agent.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class LangfuseHealthServiceTest {

    @Test
    void emptyKeysDisableExporterEvenWithEndpoint() {
        LangfuseHealthService health = LangfuseHealthService.forTest(
                "http://langfuse-web:3000/api/public/otel", "", "", "http://example:3001");
        assertFalse(health.isExporterEnabled());
        assertEquals(LangfuseHealthService.Status.AUTH_REQUIRED, health.status());
        assertEquals("", health.buildTraceUrl("abc"));
        assertFalse(health.canEmitExternalLinks());
    }

    @Test
    void missingEndpointDisablesExporter() {
        LangfuseHealthService health = LangfuseHealthService.forTest(
                "", "pk", "sk", "http://example:3001");
        assertFalse(health.isExporterEnabled());
        assertEquals(LangfuseHealthService.Status.DISABLED, health.status());
        assertTrue(health.disableReason().contains("ENDPOINT"));
    }

    @Test
    void allCredentialsEnableExporterAndExternalLink() {
        LangfuseHealthService health = LangfuseHealthService.forTest(
                "http://langfuse-web:3000/api/public/otel",
                "lf_pk_test",
                "lf_sk_test",
                "http://8.138.10.189:3001");
        assertTrue(health.isExporterEnabled());
        assertEquals(LangfuseHealthService.Status.READY, health.status());
        assertTrue(health.canEmitExternalLinks());
        assertEquals(
                "http://8.138.10.189:3001/project/resumai-project/traces/trace-1",
                health.buildTraceUrl("trace-1"));
    }

    @Test
    void missingPublicUrlBlocksExternalLinkEvenWhenEnabled() {
        LangfuseHealthService health = LangfuseHealthService.forTest(
                "http://langfuse-web:3000/api/public/otel", "pk", "sk", "");
        assertTrue(health.isExporterEnabled());
        assertFalse(health.canEmitExternalLinks());
        assertEquals("", health.buildTraceUrl("trace-1"));
        assertTrue(health.statusReason().contains("PUBLIC_URL"));
    }

    @Test
    void relativeOrBlankPublicUrlIsInvalid() {
        LangfuseHealthService health = LangfuseHealthService.forTest(
                "http://langfuse-web:3000/api/public/otel", "pk", "sk", "/langfuse");
        assertFalse(health.hasValidPublicUrl());
        assertEquals("", health.buildTraceUrl("t1"));
    }

    @Test
    void authFailureBlocksExternalLinks() {
        LangfuseHealthService health = LangfuseHealthService.forTest(
                "http://langfuse-web:3000/api/public/otel", "pk", "sk", "http://example:3001");
        health.recordExportError("HTTP 401 unauthorized");
        assertEquals(LangfuseHealthService.Status.AUTH_FAILED, health.status());
        assertFalse(health.canEmitExternalLinks());
        assertEquals("", health.buildTraceUrl("t1"));
    }

    @Test
    void redactEndpointKeepsHostPath() {
        assertEquals(
                "http://langfuse-web:3000/api/public/otel",
                LangfuseHealthService.redactEndpoint("http://langfuse-web:3000/api/public/otel"));
    }
}
