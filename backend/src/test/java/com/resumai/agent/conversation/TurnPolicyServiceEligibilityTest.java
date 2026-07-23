package com.resumai.agent.conversation;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.enums.RunStatus;
import java.lang.reflect.RecordComponent;
import java.util.Arrays;
import java.util.Set;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;

class TurnPolicyServiceEligibilityTest {

    private final TurnPolicyService service = new TurnPolicyService();

    @Test
    void public_turn_request_has_no_queue_mode() {
        Set<String> components = Arrays.stream(
                        com.resumai.agent.api.dto.ConversationTurnRequest.class.getRecordComponents())
                .map(RecordComponent::getName)
                .collect(Collectors.toSet());
        assertFalse(components.contains("queueMode"));
        assertFalse(components.contains("forcedPolicyId"));
        assertTrue(components.contains("contextRefs"));
    }

    @Test
    void conversation_direct_answer_creates_no_evaluation_disposition() {
        ConversationSession session = session();
        TurnDecision chat = service.decide(session, null, null, "1+1");
        assertEquals(TurnDisposition.DIRECT_REPLY, chat.disposition());

        TurnDecision hello = service.decide(session, null, null, "你好");
        assertEquals(TurnDisposition.DIRECT_REPLY, hello.disposition());

        TurnDecision why = service.decide(session, null, null, "为什么这个分数偏低？");
        assertEquals(TurnDisposition.DIRECT_REPLY, why.disposition());
    }

    @Test
    void explicitStopControls() {
        TurnDecision stop = service.decide(session(), active("RUNNING"), null, "停止");
        assertEquals(TurnDisposition.CONTROL, stop.disposition());
        assertEquals("CANCEL", stop.controlAction());
    }

    @Test
    void goalChangeSupersedesActiveOrCreatesRevision() {
        TurnDecision supersede = service.decide(session(), active("RUNNING"), null,
                "目标岗位改成前端，重新评估");
        assertEquals(TurnDisposition.SUPERSEDE_RUN, supersede.disposition());

        TurnDecision revision = service.decide(session(), null, null,
                "目标岗位改成前端，重新评估");
        assertEquals(TurnDisposition.CREATE_REVISION, revision.disposition());
    }

    @Test
    void factMergesIntoQueuedRunOtherwiseRevises() {
        AgentRun queued = active(RunStatus.QUEUED.name());
        TurnDecision merge = service.decide(session(), null, queued, "补充：我还有 Kafka 实战经验");
        assertEquals(TurnDisposition.MERGE_CONTEXT, merge.disposition());

        TurnDecision revise = service.decide(session(), active("RUNNING"), null,
                "补充：我还有 Kafka 实战经验");
        assertEquals(TurnDisposition.CREATE_REVISION, revise.disposition());
    }

    @Test
    void evidenceQueryIsBackground() {
        TurnDecision decision = service.decide(session(), null, null, "查一下知识库里的出处");
        assertEquals(TurnDisposition.BACKGROUND_QUERY, decision.disposition());
    }

    @Test
    void explicitEvaluationCreatesRevision() {
        TurnDecision decision = service.decide(session(), null, null, "请完整评估这份简历");
        assertEquals(TurnDisposition.CREATE_REVISION, decision.disposition());
    }

    @Test
    void handleRuntimeTurnExistsForDispositionSwitch() throws Exception {
        boolean present = Arrays.stream(com.resumai.agent.service.ConversationService.class
                        .getDeclaredMethods())
                .anyMatch(m -> m.getName().equals("handleRuntimeTurn"));
        assertTrue(present);
    }

    private ConversationSession session() {
        ConversationSession session = new ConversationSession();
        session.setId("conv-test");
        session.setResumeText("resume");
        session.setActiveRevision(1);
        return session;
    }

    private AgentRun active(String status) {
        AgentRun run = new AgentRun();
        run.setRunId("run-test");
        run.setStatus(status);
        return run;
    }
}
