package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.baomidou.mybatisplus.core.conditions.Wrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.api.dto.ConversationTurnResponse;
import com.resumai.agent.conversation.ConversationReplyService;
import com.resumai.agent.conversation.TurnDisposition;
import com.resumai.agent.conversation.TurnPolicyService;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.ConversationMessageMapper;
import com.resumai.agent.dao.ConversationSessionMapper;
import com.resumai.agent.dao.ConversationTurnMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.entity.ConversationTurn;
import com.resumai.agent.service.run.AgentRuntimeClient;
import com.resumai.agent.service.run.RunLifecycleService;
import com.resumai.agent.service.run.RunQueueService;
import com.resumai.agent.service.run.RunSchedulerService;
import com.resumai.agent.service.run.RunTypeClassifier;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.redisson.api.RMapCache;
import org.redisson.api.RedissonClient;

/**
 * Integration-style proof that DIRECT_REPLY writes conversation_turn and does
 * not create agent_run / call RunQueueService.submitEvaluationRun.
 */
@ExtendWith(MockitoExtension.class)
class ConversationDirectReplySideEffectTest {

    @Mock ConversationSessionMapper sessionMapper;
    @Mock ConversationMessageMapper messageMapper;
    @Mock ResumeTaskMapper resumeTaskMapper;
    @Mock ResumeEvaluationService evaluationService;
    @Mock TaskControlService taskControlService;
    @Mock AgentRuntimeClient runtimeClient;
    @Mock RunQueueService runQueueService;
    @Mock RunSchedulerService runSchedulerService;
    @Mock RunLifecycleService runLifecycleService;
    @Mock RunTypeClassifier runTypeClassifier;
    @Mock ConversationTurnMapper turnMapper;
    @Mock AgentRunMapper agentRunMapper;
    @Mock RedissonClient redisson;
    @Mock RMapCache<String, String> sessionCache;

    private final ObjectMapper objectMapper = new ObjectMapper();
    private final Map<String, ConversationTurn> turns = new ConcurrentHashMap<>();
    private final AtomicInteger agentRunCount = new AtomicInteger(0);

    private ConversationService conversationService;

    @BeforeEach
    void setUp() {
        lenient().when(redisson.<String, String>getMapCache(anyString())).thenReturn(sessionCache);
        lenient().when(sessionCache.get(anyString())).thenReturn(null);
        lenient().when(runtimeClient.replyConversation(any())).thenReturn(Optional.empty());
        lenient().when(runQueueService.findActiveRun(anyString())).thenReturn(null);
        lenient().when(runQueueService.findPendingRun(anyString())).thenReturn(null);
        lenient().when(messageMapper.insert(any(com.resumai.agent.domain.entity.ConversationMessage.class))).thenReturn(1);
        lenient().when(agentRunMapper.selectCount(any())).thenAnswer(inv -> (long) agentRunCount.get());

        lenient().when(turnMapper.insert(any(ConversationTurn.class))).thenAnswer(inv -> {
            ConversationTurn turn = inv.getArgument(0);
            turns.put(turn.getTurnId(), turn);
            return 1;
        });
        lenient().when(turnMapper.selectById(anyString())).thenAnswer(inv ->
                turns.get(inv.getArgument(0)));
        lenient().when(turnMapper.updateById(any(ConversationTurn.class))).thenAnswer(inv -> {
            ConversationTurn turn = inv.getArgument(0);
            turns.put(turn.getTurnId(), turn);
            return 1;
        });
        lenient().when(turnMapper.selectOne(any(Wrapper.class))).thenAnswer(inv -> null);
        lenient().when(turnMapper.selectCount(any(Wrapper.class))).thenAnswer(inv -> (long) turns.size());

        ConversationTurnService turnService = new ConversationTurnService(turnMapper, objectMapper);
        ConversationReplyService replyService = new ConversationReplyService(
                runtimeClient, agentRunMapper, messageMapper,
                org.mockito.Mockito.mock(
                        com.resumai.agent.dao.ContextSnapshotMapper.class),
                objectMapper);
        conversationService = new ConversationService(
                sessionMapper, messageMapper, resumeTaskMapper, evaluationService,
                taskControlService, new ConversationIntentClassifier(), runtimeClient,
                objectMapper, runQueueService, runSchedulerService, runLifecycleService,
                runTypeClassifier, new TurnPolicyService(), replyService, turnService, redisson);
    }

    @Test
    void directReply_onePlusOne_writesTurn_andDoesNotCreateAgentRun() {
        ConversationSession session = sessionWithResume("conv-arith-1");
        when(sessionMapper.selectById("conv-arith-1")).thenReturn(session);
        when(sessionMapper.selectOne(any(Wrapper.class))).thenReturn(session);
        when(messageMapper.selectOne(any(Wrapper.class))).thenReturn(null);

        long agentRunsBefore = agentRunMapper.selectCount(null);
        assertEquals(0L, agentRunsBefore);

        ConversationTurnResponse response = conversationService.sendTurn(
                "conv-arith-1",
                new ConversationTurnRequest("web-arith-1", "1+1", 1, null));

        assertEquals(TurnDisposition.DIRECT_REPLY.name(), response.disposition());
        assertNotNull(response.turnId());
        assertTrue(response.assistantMessage().contains("2"));
        assertNull(response.runId());
        assertEquals(0L, agentRunMapper.selectCount(null));
        assertEquals(1, turns.size());
        ConversationTurn persisted = turns.get(response.turnId());
        assertNotNull(persisted);
        assertEquals("COMPLETED", persisted.getStatus());
        assertEquals("DIRECT_REPLY", persisted.getDisposition());
        assertTrue(persisted.getAnswer().contains("2"));

        verify(runQueueService, never()).submitEvaluationRun(
                anyString(), anyString(), anyString(), org.mockito.ArgumentMatchers.anyInt(),
                anyString(), org.mockito.ArgumentMatchers.anyBoolean(), anyString(),
                org.mockito.ArgumentMatchers.any());
    }

    private static ConversationSession sessionWithResume(String id) {
        ConversationSession session = new ConversationSession();
        session.setId(id);
        session.setUserId("hr-1");
        session.setResumeText("姓名：测试\n技能：Java");
        session.setActiveTraceId("trace-1");
        session.setActiveRevision(1);
        session.setDeleted(0);
        return session;
    }
}
