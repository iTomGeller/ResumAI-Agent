package com.resumai.agent.conversation;

import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.ContextSnapshotMapper;
import com.resumai.agent.dao.ConversationMessageMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.service.run.AgentRuntimeClient;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.stream.Stream;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class ConversationReplyServiceFallbackTest {

    @Mock AgentRuntimeClient runtimeClient;
    @Mock AgentRunMapper agentRunMapper;
    @Mock ConversationMessageMapper conversationMessageMapper;
    @Mock ContextSnapshotMapper contextSnapshotMapper;

    private ConversationReplyService service;
    private ConversationSession session;

    @BeforeEach
    void setUp() {
        service = new ConversationReplyService(runtimeClient, agentRunMapper,
                conversationMessageMapper, contextSnapshotMapper, new ObjectMapper());
        session = new ConversationSession();
        session.setId("conv-fallback");
        session.setActiveRevision(3);
        when(runtimeClient.replyConversation(any())).thenReturn(Optional.empty());
    }

    @ParameterizedTest
    @MethodSource("mechanismFallbacks")
    void unavailableWorkflow_returnsDeterministicMechanismAnswer_withoutCreatingRun(
            String content, String intent, List<String> expectedFragments) {
        ConversationReplyService.CopilotReply reply = service.reply(
                session,
                new ConversationTurnRequest("client-1", content, 3, null),
                new TurnDecision(TurnDisposition.DIRECT_REPLY, intent, "test"),
                false,
                "turn-fallback");

        for (String fragment : expectedFragments) {
            assertTrue(
                    reply.answer().contains(fragment),
                    () -> "missing '" + fragment + "' in: " + reply.answer());
        }
        assertEquals("turn-fallback", reply.turnId());
        assertTrue(reply.actions().isEmpty());
        verify(agentRunMapper, never()).insert(any(AgentRun.class));
    }

    @Test
    void unavailableWorkflow_withoutReport_keepsCandidateConclusionConservative() {
        when(agentRunMapper.selectOne(any())).thenReturn(null);

        ConversationReplyService.CopilotReply reply = service.reply(
                session,
                new ConversationTurnRequest(
                        "client-candidate", "这个候选人怎么样，推荐吗？", 3, null),
                new TurnDecision(TurnDisposition.DIRECT_REPLY, "SIDE_QUESTION", "test"),
                false,
                "turn-candidate");

        assertAll(
                () -> assertTrue(reply.answer().contains("没有可用的证据化评估报告")),
                () -> assertTrue(reply.answer().contains("不能可靠地")),
                () -> assertTrue(reply.answer().contains("不会")),
                () -> assertTrue(reply.actions().isEmpty()));
        verify(agentRunMapper, never()).insert(any(AgentRun.class));
    }

    @Test
    @SuppressWarnings("unchecked")
    void workflowRequest_includesBoundedResumeAndJdContext_withoutReport() {
        session.setResumeText("R".repeat(5000));
        session.setJobDescription("J".repeat(5000));
        service.reply(
                session,
                new ConversationTurnRequest("client-context", "这个候选人的项目怎么样？", 3, null),
                new TurnDecision(TurnDisposition.DIRECT_REPLY, "SIDE_QUESTION", "test"),
                false,
                "turn-context");

        ArgumentCaptor<Map<String, Object>> bodyCaptor = ArgumentCaptor.forClass(Map.class);
        verify(runtimeClient).replyConversation(bodyCaptor.capture());
        Map<String, Object> snapshot =
                (Map<String, Object>) bodyCaptor.getValue().get("contextSnapshot");

        assertAll(
                () -> assertEquals(1800, String.valueOf(snapshot.get("resumeText")).length()),
                () -> assertEquals(
                        1600, String.valueOf(snapshot.get("jobDescription")).length()),
                () -> assertEquals(true, snapshot.get("hasResume")),
                () -> assertEquals(true, snapshot.get("hasJobDescription")));
    }

    private static Stream<Arguments> mechanismFallbacks() {
        return Stream.of(
                Arguments.of(
                        "RAG 的多阶段检索和指标是什么？",
                        "SIDE_QUESTION",
                        List.of("没有实际执行 RAG", "Query Rewrite", "重排", "降级原因")),
                Arguments.of(
                        "checkpoint 暂停恢复怎么工作？",
                        "SIDE_QUESTION",
                        List.of("安全边界", "同一个 Run", "同一个 revision", "不会改变运行状态")),
                Arguments.of(
                        "revision 和 JD 重点修改后怎么重跑？",
                        "SIDE_QUESTION",
                        List.of("新 revision", "已取代", "受影响节点", "不会创建 revision 或 Run")),
                Arguments.of(
                        "MCP 是怎么让模型选工具和参数的？",
                        "SIDE_QUESTION",
                        List.of("tools/list", "input schema", "tools/call", "没有实际调用 MCP")),
                Arguments.of(
                        "证据不足时会硬给分吗？",
                        "SIDE_QUESTION",
                        List.of("UNASSESSED", "不会补猜", "人工复核")),
                Arguments.of(
                        "请查一下 Kafka 项目的公开证据",
                        "EVIDENCE_QUERY",
                        List.of("没有实际执行 RAG", "不会伪造命中", "重试原问题")));
    }
}
