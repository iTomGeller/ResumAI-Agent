package com.resumai.agent.service.run;

import static org.junit.jupiter.api.Assertions.assertSame;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.baomidou.mybatisplus.core.conditions.Wrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.AgentRunProperties;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.enums.RunStatus;
import java.time.LocalDateTime;
import java.util.List;
import org.junit.jupiter.api.Test;

class RunQueueServiceStateSnapshotTest {

    @Test
    void resolvesActivePausedAndPendingWithOneMapperQuery() {
        AgentRunMapper mapper = mock(AgentRunMapper.class);
        RunQueueService service = new RunQueueService(
                mapper, mock(RunEventService.class),
                new AgentRunProperties(), new ObjectMapper());
        AgentRun queued = run("queued", RunStatus.QUEUED, 3);
        AgentRun olderActive = run("older-active", RunStatus.RUNNING, 4);
        AgentRun paused = run("paused", RunStatus.PAUSED, 2);
        AgentRun active = run("active", RunStatus.WAITING_LLM, 1);
        when(mapper.selectList(any(Wrapper.class)))
                .thenReturn(List.of(queued, active, paused, olderActive));

        RunQueueService.ConversationRunState state =
                service.findConversationRunState("conv-1");

        assertSame(active, state.active());
        assertSame(paused, state.paused());
        assertSame(queued, state.pending());
        verify(mapper, times(1)).selectList(any(Wrapper.class));
    }

    private static AgentRun run(String id, RunStatus status, int seconds) {
        AgentRun run = new AgentRun();
        run.setRunId(id);
        run.setStatus(status.name());
        run.setCreatedAt(LocalDateTime.now().minusSeconds(seconds));
        return run;
    }
}
