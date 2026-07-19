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
}
