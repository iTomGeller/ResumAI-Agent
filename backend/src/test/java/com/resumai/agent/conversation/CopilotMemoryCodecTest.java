package com.resumai.agent.conversation;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.domain.entity.ConversationSession;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class CopilotMemoryCodecTest {

    private final CopilotMemoryCodec codec =
            new CopilotMemoryCodec(new ObjectMapper());

    @Test
    void persistsStructuredMemoryAndRestoresOnlyMatchingScope() {
        ConversationSession session = session(2, "resume-v2", "jd-v1");
        CopilotMemoryCodec.Scope scope = codec.scopeFor(session);
        Map<String, Object> provider = Map.of(
                "goals", List.of(Map.of(
                        "text", "判断Java后端岗位匹配度",
                        "sourceMessageId", 101)),
                "decisions", List.of(Map.of(
                        "text", "性能提升缺少测试基线",
                        "sourceMessageId", 102)),
                "openQuestions", List.of("个人负责的Kafka模块是什么"),
                "evidenceRefs", List.of("resume:project:2"));

        Map<String, Object> stored = codec.mergeForStorage(
                Map.of(), provider, scope, 120L);
        String encoded = codec.encode(stored);
        Map<String, Object> restored = codec.decodeMatching(encoded, scope);

        assertEquals(CopilotMemoryCodec.SCHEMA_VERSION,
                restored.get("schemaVersion"));
        assertEquals(120L, restored.get("compactedThroughMessageId"));
        assertFalse(((List<?>) restored.get("goals")).isEmpty());
        assertEquals(List.of("resume:project:2"), restored.get("evidenceRefs"));

        CopilotMemoryCodec.Scope changed = codec.scopeFor(
                session(3, "resume-v3", "jd-v1"));
        assertTrue(codec.decodeMatching(encoded, changed).isEmpty());
    }

    @Test
    void mergeIsCumulativeAndDeduplicatesByText() {
        CopilotMemoryCodec.Scope scope = codec.scopeFor(
                session(1, "resume", "jd"));
        Map<String, Object> previous = codec.mergeForStorage(
                Map.of(),
                Map.of("decisions", List.of(Map.of(
                        "text", "缺少性能基线",
                        "sourceMessageId", 10))),
                scope, 10L);
        Map<String, Object> merged = codec.mergeForStorage(
                previous,
                Map.of("decisions", List.of(
                        Map.of("text", "缺少性能基线", "sourceMessageId", 11),
                        Map.of("text", "职责边界待确认", "sourceMessageId", 12))),
                scope, 12L);

        List<?> decisions = (List<?>) merged.get("decisions");
        assertEquals(2, decisions.size());
        assertEquals(12L, merged.get("compactedThroughMessageId"));
    }

    @Test
    void legacyPlainTextSummaryFailsClosed() {
        CopilotMemoryCodec.Scope scope = codec.scopeFor(
                session(1, "resume", "jd"));
        assertTrue(codec.decodeMatching("旧的一段自然语言摘要", scope).isEmpty());
    }

    private static ConversationSession session(
            int revision, String resume, String jd) {
        ConversationSession session = new ConversationSession();
        session.setId("conv-test");
        session.setActiveRevision(revision);
        session.setResumeText(resume);
        session.setJobDescription(jd);
        return session;
    }
}
