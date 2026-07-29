package com.resumai.agent.service.ops;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.resumai.agent.api.dto.ops.OpsDebugDtos.MemoryTtlView;
import com.resumai.agent.domain.entity.MemoryEntryRow;
import com.resumai.agent.service.MemoryService;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;

class OpsMemoryTtlTest {

    @Test
    void exposesEffectiveRemainingAndOverrideWithoutRenewal() {
        LocalDateTime created = LocalDateTime.of(2026, 1, 1, 0, 0);
        MemoryEntryRow row = row("SEMANTIC", "ACTIVE", created, created.plusDays(180));

        MemoryTtlView ttl = OpsDebugService.memoryTtlView(row, created.plusDays(80));

        assertEquals("ABSOLUTE", ttl.mode());
        assertEquals("ACTIVE", ttl.state());
        assertEquals(180L * 24 * 60 * 60, ttl.effectiveTtlSeconds());
        assertEquals(100L * 24 * 60 * 60, ttl.remainingTtlSeconds());
        assertEquals(90L, ttl.typeDefaultDays());
        assertTrue(ttl.overrideDetected());
        assertFalse(ttl.renewOnUse());
    }

    @Test
    void reportsExpiringAndExpiredStatesFromWallClock() {
        LocalDateTime created = LocalDateTime.of(2026, 1, 1, 0, 0);
        MemoryEntryRow row = row("EPISODIC", "ACTIVE", created, created.plusDays(90));

        assertEquals("EXPIRING_SOON",
                OpsDebugService.memoryTtlView(row, created.plusDays(85)).state());
        assertEquals("EXPIRED",
                OpsDebugService.memoryTtlView(row, created.plusDays(91)).state());
        assertFalse(OpsDebugService.memoryTtlView(
                row, created.plusDays(85)).overrideDetected());
    }

    @Test
    void publishesCanonicalTypeDefaults() {
        assertEquals(2L, MemoryService.ttlPolicyDays().get("WORKING"));
        assertEquals(90L, MemoryService.ttlPolicyDays().get("SEMANTIC"));
        assertEquals(90L, MemoryService.ttlPolicyDays().get("EPISODIC"));
        assertEquals(365L, MemoryService.ttlPolicyDays().get("PROCEDURAL"));
    }

    @Test
    void computesUsageAgeAcrossLocalMemoryAndUtcUsageStorage() {
        MemoryEntryRow row = row(
                "PROCEDURAL", "ACTIVE",
                LocalDateTime.of(2026, 7, 29, 13, 0),
                LocalDateTime.of(2027, 7, 29, 13, 0));

        LocalDateTime usageUtc = LocalDateTime.ofInstant(
                row.getCreateTime().atZone(ZoneId.systemDefault()).toInstant().plusSeconds(7200),
                ZoneOffset.UTC);
        Long age = OpsDebugService.memoryAgeAtUseSeconds(row, usageUtc);

        assertEquals(2L * 60 * 60, age);
    }

    private static MemoryEntryRow row(String type, String status,
                                      LocalDateTime created, LocalDateTime expires) {
        MemoryEntryRow row = new MemoryEntryRow();
        row.setType(type);
        row.setStatus(status);
        row.setCreateTime(created);
        row.setExpiresAt(expires);
        return row;
    }
}
