package com.resumai.agent.service.run;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.RunEvent;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class RunTraceBridgeServiceCausalityTest {

    private final ObjectMapper json = new ObjectMapper();

    @Test
    @SuppressWarnings("unchecked")
    void modelInputsAndNativeToolsAreChildrenOfTheirLlmRound() throws Exception {
        String agent = "ProjectAgent";
        List<RunEvent> events = List.of(
                event(1, "agent.started", agent, null,
                        Map.of("description", "项目经历评估",
                                "occurredAt", "2026-07-28T12:00:00.000Z")),
                event(2, "tool.started", agent, "parse_resume",
                        Map.of("toolCallId", "pre-1", "arguments", Map.of("text", "cv"),
                                "occurredAt", "2026-07-28T12:00:00.010Z")),
                event(3, "tool.completed", agent, "parse_resume",
                        Map.of("toolCallId", "pre-1", "resultPreview", Map.of("ok", true),
                                "occurredAt", "2026-07-28T12:00:00.020Z")),
                event(4, "agent.progress", agent, null,
                        Map.of("iteration", 1, "roundId", "round-1")),
                event(5, "memory.used", agent, "memory_retrieval",
                        Map.of("roundId", "round-1", "memoryId", "mem-1",
                                "taxonomy", "PROCEDURAL", "namespace", "user:u1",
                                "occurredAt", "2026-07-28T12:00:00.030Z")),
                event(6, "memory.used", agent, "memory_retrieval",
                        Map.of("roundId", "round-1", "memoryId", "mem-1",
                                "taxonomy", "PROCEDURAL", "namespace", "user:u1",
                                "occurredAt", "2026-07-28T12:00:00.031Z")),
                event(7, "llm.context.attached", agent, null,
                        Map.of(
                                "roundId", "round-1",
                                "contextRole", "MODEL_INPUT",
                                "memoryCount", 1,
                                "skillCount", 1,
                                "toolCatalogCount", 1,
                                "memoryRefs", List.of(Map.of(
                                        "id", "mem-1", "type", "PROCEDURAL",
                                        "namespace", "user:u1", "score", 0.91)),
                                "skillRefs", List.of(Map.of(
                                        "skillId", "ground-project-claims",
                                        "skillVersion", "v1", "selected", true)),
                                "toolCatalogRefs", List.of(Map.of(
                                        "name", "exa.web_search_exa", "source", "mcp",
                                        "mcpServer", "exa", "modelName", "exa_web_search_exa")),
                                "occurredAt", "2026-07-28T12:00:00.040Z")),
                event(8, "llm.started", agent, null,
                        Map.of("roundId", "round-1", "callIndex", 1,
                                "model", "deepseek-chat", "purpose", "project_findings",
                                "occurredAt", "2026-07-28T12:00:00.050Z")),
                event(9, "llm.completed", agent, null,
                        Map.of("roundId", "round-1", "callIndex", 1,
                                "model", "deepseek-chat", "promptTokens", 100,
                                "completionTokens", 20, "durationMs", 900,
                                "toolCallCount", 1,
                                "toolNames", List.of("exa_web_search_exa"),
                                "occurredAt", "2026-07-28T12:00:00.950Z")),
                event(10, "tool.progress", agent, "exa.web_search_exa",
                        Map.of("roundId", "round-1", "toolCallId", "tc-1",
                                "lifecycleStage", "LLM_PROPOSED", "source", "mcp",
                                "mcpServer", "exa", "modelName", "exa_web_search_exa",
                                "arguments", Map.of("query", "candidate github"),
                                "occurredAt", "2026-07-28T12:00:00.960Z")),
                event(11, "tool.started", agent, "exa.web_search_exa",
                        Map.of("roundId", "round-1", "toolCallId", "tc-1",
                                "lifecycleStage", "EXECUTION_STARTED", "source", "mcp",
                                "mcpServer", "exa", "arguments", Map.of("query", "candidate github"),
                                "occurredAt", "2026-07-28T12:00:00.970Z")),
                event(12, "tool.completed", agent, "exa.web_search_exa",
                        Map.of("roundId", "round-1", "toolCallId", "tc-1",
                                "lifecycleStage", "RESULT", "source", "mcp",
                                "mcpServer", "exa", "durationMs", 80,
                                "resultPreview", Map.of("success", true),
                                "occurredAt", "2026-07-28T12:00:01.050Z")),
                event(13, "agent.progress", agent, null,
                        Map.of("iteration", 2, "roundId", "round-2")),
                event(14, "llm.context.attached", agent, null,
                        Map.of("roundId", "round-2", "skillCount", 1,
                                "observedToolCallIds", List.of("tc-1"),
                                "skillRefs", List.of(Map.of(
                                        "skillId", "ground-project-claims",
                                        "skillVersion", "v1", "loaded", true, "applied", true)),
                                "occurredAt", "2026-07-28T12:00:01.060Z")),
                event(15, "skill.applied", agent, "ground-project-claims",
                        Map.of("applicationRoundId", "round-2", "toolCallId", "tc-skill",
                                "skillId", "ground-project-claims", "skillVersion", "v1",
                                "lifecycleStage", "APPLIED",
                                "occurredAt", "2026-07-28T12:00:01.061Z")),
                event(16, "llm.started", agent, null,
                        Map.of("roundId", "round-2", "callIndex", 2,
                                "occurredAt", "2026-07-28T12:00:01.070Z")),
                event(17, "llm.completed", agent, null,
                        Map.of("roundId", "round-2", "callIndex", 2,
                                "promptTokens", 80, "completionTokens", 10,
                                "durationMs", 500,
                                "occurredAt", "2026-07-28T12:00:01.570Z")),
                event(18, "skill.skipped", agent, "unused-skill",
                        Map.of("skillId", "unused-skill", "lifecycleStage", "SKIPPED")),
                event(19, "agent.completed", agent, null,
                        Map.of("llmCalls", 2, "toolCalls", 2, "durationMs", 1570))
        );

        Map<String, Object> tree = bridge(events).executionTreeForTrace("trace-1");
        List<Map<String, Object>> agents = (List<Map<String, Object>>) tree.get("executionTree");
        Map<String, Object> node = agents.get(0);
        List<Map<String, Object>> rounds = (List<Map<String, Object>>) node.get("rounds");

        assertEquals(2, rounds.size());
        assertTrue(rounds.stream().allMatch(round -> "generation".equals(round.get("type"))));

        Map<String, Object> first = rounds.get(0);
        assertEquals("round-1", first.get("roundId"));
        assertEquals("2026-07-28T12:00:00.050Z", first.get("startedAt"));
        assertEquals("2026-07-28T12:00:00.950Z", first.get("endedAt"));
        List<Map<String, Object>> firstContext =
                (List<Map<String, Object>>) first.get("contextEvents");
        assertEquals(3, firstContext.size(), "memory is deduplicated; skill and MCP catalog stay attached");
        assertEquals(1, firstContext.stream()
                .filter(item -> "memory".equals(item.get("category"))).count());

        List<Map<String, Object>> toolCalls =
                (List<Map<String, Object>>) first.get("toolCalls");
        assertEquals(1, toolCalls.size());
        Map<String, Object> mcp = toolCalls.get(0);
        assertEquals("tc-1", mcp.get("toolCallId"));
        assertEquals("round-1", mcp.get("parentRoundId"));
        assertEquals("mcp", mcp.get("category"));
        assertEquals("2026-07-28T12:00:00.970Z", mcp.get("startedAt"));
        assertEquals("2026-07-28T12:00:01.050Z", mcp.get("endedAt"));
        assertTrue(((List<String>) mcp.get("lifecycle")).containsAll(
                List.of("LLM_PROPOSED", "EXECUTION_STARTED", "RESULT")));

        List<Map<String, Object>> secondContext =
                (List<Map<String, Object>>) rounds.get(1).get("contextEvents");
        assertEquals(1, secondContext.size(), "skill attached + applied lifecycle must not duplicate");
        assertEquals("skill", secondContext.get(0).get("category"));
        assertEquals(List.of("tc-1"), rounds.get(1).get("observedToolCallIds"));

        List<Map<String, Object>> deterministic =
                (List<Map<String, Object>>) node.get("deterministicSteps");
        assertEquals(1, deterministic.size());
        assertEquals("parse_resume", deterministic.get(0).get("name"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void deterministicPreflightDoesNotRenderAsLlmRound() throws Exception {
        String agent = "CoordinatorAgent";
        List<RunEvent> events = List.of(
                event(1, "agent.started", agent, null, Map.of("description", "确定性预处理")),
                event(2, "tool.started", agent, "parse_resume",
                        Map.of("toolCallId", "pre-parse", "arguments", Map.of("text", "cv"))),
                event(3, "tool.completed", agent, "parse_resume",
                        Map.of("toolCallId", "pre-parse", "resultPreview", Map.of("ok", true))),
                event(4, "agent.completed", agent, null,
                        Map.of("llmCalls", 0, "toolCalls", 1, "durationMs", 20))
        );

        Map<String, Object> tree = bridge(events).executionTreeForTrace("trace-1");
        Map<String, Object> node = ((List<Map<String, Object>>) tree.get("executionTree")).get(0);
        assertTrue(((List<?>) node.get("rounds")).isEmpty());
        assertEquals("deterministic", node.get("executionMode"));
        assertEquals(1, ((List<?>) node.get("deterministicSteps")).size());
        assertFalse(node.toString().contains("route-conversation-turn"));
    }

    private RunTraceBridgeService bridge(List<RunEvent> events) {
        AgentRunMapper runs = mock(AgentRunMapper.class);
        RunEventService eventService = mock(RunEventService.class);
        AgentRun run = new AgentRun();
        run.setRunId("run-1");
        run.setTraceId("trace-1");
        run.setSourceTaskTraceId("trace-1");
        run.setStatus("SUCCEEDED");
        run.setRetryCount(0);
        when(runs.selectOne(any())).thenReturn(run);
        when(runs.selectList(any())).thenReturn(List.of());
        when(eventService.listSince(anyString(), anyInt(), anyInt())).thenReturn(events);
        return new RunTraceBridgeService(runs, eventService, json);
    }

    private RunEvent event(long id, String type, String agent,
                           String tool, Map<String, Object> payload) throws Exception {
        RunEvent event = new RunEvent();
        event.setId(id);
        event.setRunId("run-1");
        event.setTraceId("trace-1");
        event.setSeq((int) id);
        event.setEventType(type);
        event.setAgentId(agent);
        event.setToolName(tool);
        event.setPayload(json.writeValueAsString(new LinkedHashMap<>(payload)));
        event.setCreateTime(LocalDateTime.of(2026, 7, 28, 12, 0)
                .plusNanos(id * 1_000_000));
        return event;
    }
}
