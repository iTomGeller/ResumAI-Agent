package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import org.junit.jupiter.api.Test;

class MemoryTaxonomyTest {

    @Test
    void legacyNamesNormalizeToCanonicalTaxonomy() {
        assertEquals("WORKING", MemoryService.canonicalTaxonomy("CONVERSATION"));
        assertEquals("SEMANTIC", MemoryService.canonicalTaxonomy("PREFERENCE"));
        assertEquals("SEMANTIC", MemoryService.canonicalTaxonomy("DOMAIN"));
        assertEquals("EPISODIC", MemoryService.canonicalTaxonomy("FAILURE"));
        assertEquals("PROCEDURAL", MemoryService.canonicalTaxonomy("PROCEDURAL"));
    }

    @Test
    void agentRoleProducesDifferentAllowedMemoryTypes() {
        var parser = MemoryService.retrievalPlan(
                null, "ResumeParserAgent", "解析当前候选人简历事实");
        var report = MemoryService.retrievalPlan(
                null, "ReportAgent", "参考上次评估结果生成报告");
        var coordinator = MemoryService.retrievalPlan(
                null, "CoordinatorAgent", "采用已批准的评分规则");

        assertEquals(List.of("SEMANTIC", "WORKING"), parser.allowedTypes());
        assertTrue(report.allowedTypes().contains("EPISODIC"));
        assertFalse(report.allowedTypes().contains("WORKING"));
        assertEquals(List.of("WORKING", "SEMANTIC", "PROCEDURAL", "EPISODIC"),
                coordinator.allowedTypes());
    }

    @Test
    void queryIntentChangesTaxonomyPreferenceWithoutWideningScope() {
        var facts = MemoryService.retrievalPlan(
                null, "TechAgent", "候选人的 Java 技能和项目经历");
        var history = MemoryService.retrievalPlan(
                null, "TechAgent", "对比上次评估结果和历史经验");
        var rules = MemoryService.retrievalPlan(
                null, "TechAgent", "评分标准和审核流程是什么");

        assertEquals("SEMANTIC", facts.preferredTypes().getFirst());
        assertEquals("EPISODIC", history.preferredTypes().getFirst());
        assertEquals("PROCEDURAL", rules.preferredTypes().getFirst());
        assertEquals(facts.allowedTypes(), history.allowedTypes());
        assertEquals(history.allowedTypes(), rules.allowedTypes());
    }
}
