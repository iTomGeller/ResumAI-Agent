package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.resumai.agent.service.candidate.CandidateIdentityExtractor;
import com.resumai.agent.service.candidate.IdentityHints;
import org.junit.jupiter.api.Test;

class CandidateIdentityHeuristicTest {

    private final CandidateIdentityExtractor extractor = new CandidateIdentityExtractor();

    @Test
    void prefersEmailAsIdentityKey() {
        String text = """
                张三
                Email: zhang.san@example.com
                电话: 13800138000
                工作经历...
                """;
        IdentityHints hints = extractor.extract(text, "resume.pdf");
        assertEquals("EMAIL", hints.identitySource());
        assertEquals("email:zhang.san@example.com", hints.identityKey());
        assertEquals("zhang.san@example.com", hints.email());
        assertEquals("13800138000", hints.phone());
        assertTrue(hints.identityConfidence() >= 0.9);
    }

    @Test
    void fallsBackToPhoneThenLegacyForAnonymousBody() {
        String withPhone = "联系方式 13912345678\n后端工程师";
        IdentityHints phoneHints = extractor.extract(withPhone, "a.pdf");
        assertEquals("PHONE", phoneHints.identitySource());
        assertTrue(phoneHints.identityKey().startsWith("phone:"));

        IdentityHints anon = extractor.extract(
                "some anonymous blob without contacts or names here at all",
                "anon.pdf",
                "trace-anon");
        assertEquals("LEGACY_UNVERIFIED", anon.identitySource());
        assertEquals("legacy:trace:trace-anon", anon.identityKey());
    }

    @Test
    void sameEmailYieldsSameIdentityKey() {
        IdentityHints a = extractor.extract("Name: Alice\nalice@corp.io\n", "a.pdf");
        IdentityHints b = extractor.extract("Alice\nMobile 100\nalice@corp.io", "b.pdf");
        assertEquals(a.identityKey(), b.identityKey());
    }

    @Test
    void rejectsReservedHeadingAsName() {
        String text = """
                基本信息
                精通 Java 与 Spring Boot
                """;
        IdentityHints hints = extractor.extract(text, "resume.pdf", "trace-heading");
        assertNotEquals("基本信息", hints.displayName());
        assertFalse("基本信息".equals(hints.displayName()));
        // 无邮箱电话且无可用姓名 -> legacy
        assertTrue(hints.identityKey().startsWith("legacy:trace:")
                || hints.identityKey().startsWith("name:")
                || hints.identityKey().startsWith("hash:"));
        if (hints.identityKey().startsWith("legacy:trace:")) {
            assertEquals("LEGACY_UNVERIFIED", hints.identitySource());
            assertEquals("legacy:trace:trace-heading", hints.identityKey());
        }
    }

    @Test
    void extractsPersonFromChineseResumeFileName() {
        assertEquals("黄义健", CandidateIdentityExtractor.extractPersonFromFileName("黄义健的简历 (4).pdf"));
        assertEquals("黄义健", CandidateIdentityExtractor.extractPersonFromFileName("黄义健简历.pdf"));
        IdentityHints hints = extractor.extract("", "黄义健的简历 (4).pdf", "trace-fn-1");
        assertEquals("黄义健", hints.displayName());
        assertEquals("name:file:黄义健", hints.identityKey());
        assertEquals("FILE_NAME", hints.identitySource());
    }

    @Test
    void emptyTextDoesNotShareEmptyHash() {
        IdentityHints a = extractor.extract("", "a.pdf", "trace-1");
        IdentityHints b = extractor.extract("   ", "b.pdf", "trace-2");
        assertEquals("legacy:trace:trace-1", a.identityKey());
        assertEquals("legacy:trace:trace-2", b.identityKey());
        assertNotEquals(a.identityKey(), b.identityKey());
        assertEquals("LEGACY_UNVERIFIED", a.identitySource());
    }
}
