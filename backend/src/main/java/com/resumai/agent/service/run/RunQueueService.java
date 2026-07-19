package com.resumai.agent.service.run;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.AgentRunProperties;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.enums.RunStatus;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Durable run queue on MySQL: rows in status QUEUED are the queue, ordered by
 * created_at within each conversation. COLLECT merges consecutive supplements
 * into the pending run; INTERRUPT cancels the active run and supersedes the
 * queue tail with a fresh run carrying the latest intent.
 */
@Service
public class RunQueueService {

    private static final Logger log = LoggerFactory.getLogger(RunQueueService.class);

    private final AgentRunMapper runMapper;
    private final RunEventService eventService;
    private final AgentRunProperties properties;
    private final ObjectMapper objectMapper;

    public RunQueueService(AgentRunMapper runMapper,
                           RunEventService eventService,
                           AgentRunProperties properties,
                           ObjectMapper objectMapper) {
        this.runMapper = runMapper;
        this.eventService = eventService;
        this.properties = properties;
        this.objectMapper = objectMapper;
    }

    public record SubmitResult(AgentRun run, boolean mergedIntoExisting, AgentRun interruptedRun) {
    }

    /**
     * Enqueue one conversation turn as a run (or merge it into the pending
     * COLLECT run). Caller must already hold the conversation row lock so the
     * merge check cannot race with another turn of the same conversation.
     */
    public SubmitResult submit(String conversationId, String userId, String traceId,
                               int revision, String runType, String queueMode,
                               String userMessage, Long messageId) {
        return submit(conversationId, userId, traceId, revision, runType,
                queueMode, userMessage, messageId, null);
    }

    /**
     * @param forcedPolicyId non-null only for benchmark/replay turns: pins the
     *                       policy (recorded as a FORCED selection at start).
     */
    public SubmitResult submit(String conversationId, String userId, String traceId,
                               int revision, String runType, String queueMode,
                               String userMessage, Long messageId,
                               String forcedPolicyId) {
        boolean interrupt = "interrupt".equalsIgnoreCase(queueMode);
        AgentRun active = findActiveRun(conversationId);
        AgentRun interrupted = null;

        if (interrupt && active != null) {
            interrupted = requestCancel(active.getRunId(),
                    "interrupted_by_new_message", "INTERRUPT 新消息取代当前运行");
        }

        if (!interrupt) {
            AgentRun pending = findPendingRun(conversationId);
            if (pending != null && mergeIntoPending(pending, userMessage, messageId)) {
                eventService.publish(pending.getRunId(), conversationId, pending.getTraceId(),
                        "run.queued", null, null, Map.of(
                                "merged", true,
                                "queuePosition", queuePosition(pending),
                                "messagePreview", preview(userMessage)));
                return new SubmitResult(runMapper.selectById(pending.getRunId()), true, null);
            }
        } else {
            // The superseded queue: fold any queued messages into the new run
            // so no user content is silently dropped.
            List<AgentRun> queued = listQueued(conversationId);
            List<String> mergedIds = new ArrayList<>();
            StringBuilder mergedText = new StringBuilder();
            for (AgentRun stale : queued) {
                if (cancelQueued(stale.getRunId(), "superseded_by_interrupt")) {
                    mergedIds.add(stale.getRunId());
                    if (StringUtils.hasText(stale.getUserMessage())) {
                        mergedText.append(stale.getUserMessage()).append("\n\n");
                    }
                    eventService.publish(stale.getRunId(), conversationId, stale.getTraceId(),
                            "run.cancelled", null, null,
                            Map.of("reason", "superseded_by_interrupt"));
                }
            }
            if (mergedText.length() > 0) {
                userMessage = mergedText + "[最新指令] " + userMessage;
            }
        }

        AgentRun run = new AgentRun();
        run.setRunId("run-" + UUID.randomUUID());
        run.setConversationId(conversationId);
        run.setUserId(StringUtils.hasText(userId) ? userId : "demo-hr");
        run.setTraceId(StringUtils.hasText(traceId) ? traceId : "rt-" + UUID.randomUUID());
        run.setRevisionNo(Math.max(1, revision));
        run.setRunType(runType);
        run.setQueueMode(interrupt ? "interrupt" : "collect");
        run.setUserMessage(userMessage);
        run.setMergedMessageIds(writeJson(messageId != null ? List.of(messageId) : List.of()));
        run.setStatus(RunStatus.QUEUED.name());
        run.setRetryCount(0);
        if (StringUtils.hasText(forcedPolicyId)) {
            run.setPolicyId(forcedPolicyId); // pre-pinned; startRun records FORCED
        }
        LocalDateTime now = LocalDateTime.now();
        run.setCreatedAt(now);
        run.setUpdatedAt(now);
        run.setTimeoutAt(now.plusSeconds(properties.getRunTimeoutSeconds()));
        run.setDeleted(0);
        runMapper.insert(run);
        eventService.publish(run.getRunId(), conversationId, run.getTraceId(),
                "run.queued", null, null, Map.of(
                        "queuePosition", queuePosition(run),
                        "runType", runType,
                        "queueMode", run.getQueueMode()));
        return new SubmitResult(run, false, interrupted);
    }

    /**
     * Direct enqueue used by the legacy resume_task bridge: one uploaded-resume
     * evaluation becomes one queued run tied back to its task via
     * sourceTaskTraceId. Same queue, same permits, same lifecycle.
     */
    public AgentRun enqueueTaskRun(String runId, String conversationId, String userId,
                                   String traceId, int revision, String runType,
                                   String userMessage, String sourceTaskTraceId,
                                   int timeoutSeconds) {
        AgentRun existing = StringUtils.hasText(runId) ? runMapper.selectById(runId) : null;
        if (existing != null) {
            return existing; // idempotent re-dispatch of the same task
        }
        AgentRun run = new AgentRun();
        run.setRunId(StringUtils.hasText(runId) ? runId : "run-" + UUID.randomUUID());
        run.setConversationId(conversationId);
        run.setUserId(StringUtils.hasText(userId) ? userId : "demo-hr");
        run.setTraceId(StringUtils.hasText(traceId) ? traceId : "rt-" + UUID.randomUUID());
        run.setRevisionNo(Math.max(1, revision));
        run.setRunType(StringUtils.hasText(runType) ? runType : "full_evaluation");
        run.setQueueMode("collect");
        run.setUserMessage(userMessage);
        run.setMergedMessageIds("[]");
        run.setStatus(RunStatus.QUEUED.name());
        run.setRetryCount(0);
        run.setSourceTaskTraceId(sourceTaskTraceId);
        LocalDateTime now = LocalDateTime.now();
        run.setCreatedAt(now);
        run.setUpdatedAt(now);
        run.setTimeoutAt(now.plusSeconds(
                timeoutSeconds > 0 ? timeoutSeconds : properties.getRunTimeoutSeconds()));
        run.setDeleted(0);
        runMapper.insert(run);
        eventService.publish(run.getRunId(), conversationId, run.getTraceId(),
                "run.queued", null, null, Map.of(
                        "queuePosition", queuePosition(run),
                        "runType", run.getRunType(),
                        "queueMode", "collect",
                        "sourceTaskTraceId", sourceTaskTraceId != null ? sourceTaskTraceId : ""));
        return run;
    }

    /** The earliest QUEUED run for the conversation (its next unit of work). */
    public AgentRun findPendingRun(String conversationId) {
        return runMapper.selectOne(new QueryWrapper<AgentRun>()
                .eq("conversation_id", conversationId)
                .eq("status", RunStatus.QUEUED.name())
                .orderByDesc("created_at")
                .last("limit 1"));
    }

    public AgentRun findActiveRun(String conversationId) {
        return runMapper.selectOne(new QueryWrapper<AgentRun>()
                .eq("conversation_id", conversationId)
                .in("status", RunStatus.ACTIVE)
                .orderByDesc("created_at")
                .last("limit 1"));
    }

    public List<AgentRun> listQueued(String conversationId) {
        return runMapper.selectList(new QueryWrapper<AgentRun>()
                .eq("conversation_id", conversationId)
                .eq("status", RunStatus.QUEUED.name())
                .orderByAsc("created_at"));
    }

    /** @return true when the message was folded into the still-queued run. */
    private boolean mergeIntoPending(AgentRun pending, String userMessage, Long messageId) {
        List<Object> ids = readJsonList(pending.getMergedMessageIds());
        if (messageId != null) {
            ids.add(messageId);
        }
        String combined = StringUtils.hasText(pending.getUserMessage())
                ? pending.getUserMessage() + "\n\n[补充] " + userMessage
                : userMessage;
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", pending.getRunId())
                .eq("status", RunStatus.QUEUED.name())
                .set("user_message", combined)
                .set("merged_message_ids", writeJson(ids))
                .set("updated_at", LocalDateTime.now());
        boolean merged = runMapper.update(null, update) > 0;
        if (!merged) {
            log.info("collect merge raced with dispatch run={}, creating a fresh run", pending.getRunId());
        }
        return merged;
    }

    /** CAS QUEUED -> CANCELLED for runs that never started. */
    public boolean cancelQueued(String runId, String reason) {
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .eq("status", RunStatus.QUEUED.name())
                .set("status", RunStatus.CANCELLED.name())
                .set("cancellation_reason", reason)
                .set("finished_at", LocalDateTime.now())
                .set("updated_at", LocalDateTime.now());
        return runMapper.update(null, update) > 0;
    }

    /** CAS an active run into CANCELLING (cancel propagation happens in the lifecycle service). */
    public AgentRun requestCancel(String runId, String reasonCode, String reasonText) {
        UpdateWrapper<AgentRun> update = new UpdateWrapper<>();
        update.eq("run_id", runId)
                .in("status", RunStatus.ACTIVE)
                .ne("status", RunStatus.CANCELLING.name())
                .set("status", RunStatus.CANCELLING.name())
                .set("error_code", reasonCode)
                .set("cancellation_reason", reasonText)
                .set("updated_at", LocalDateTime.now());
        runMapper.update(null, update);
        return runMapper.selectById(runId);
    }

    public int queuePosition(AgentRun run) {
        if (!RunStatus.QUEUED.name().equals(run.getStatus())) {
            return 0;
        }
        Long ahead = runMapper.selectCount(new QueryWrapper<AgentRun>()
                .eq("status", RunStatus.QUEUED.name())
                .lt("created_at", run.getCreatedAt()));
        return (ahead != null ? ahead.intValue() : 0) + 1;
    }

    public List<AgentRun> listQueuedGlobal(int limit) {
        return runMapper.selectList(new QueryWrapper<AgentRun>()
                .eq("status", RunStatus.QUEUED.name())
                .orderByAsc("created_at")
                .last("limit " + Math.max(1, limit)));
    }

    private String preview(String text) {
        if (text == null) {
            return "";
        }
        return text.length() > 120 ? text.substring(0, 120) + "..." : text;
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "[]";
        }
    }

    private List<Object> readJsonList(String json) {
        try {
            if (!StringUtils.hasText(json)) {
                return new ArrayList<>();
            }
            return new ArrayList<>(objectMapper.readValue(json, List.class));
        } catch (Exception e) {
            return new ArrayList<>();
        }
    }

    public Map<String, Object> queueSnapshot() {
        Map<String, Object> snapshot = new LinkedHashMap<>();
        Long queued = runMapper.selectCount(new QueryWrapper<AgentRun>()
                .eq("status", RunStatus.QUEUED.name()));
        Long active = runMapper.selectCount(new QueryWrapper<AgentRun>()
                .in("status", RunStatus.ACTIVE));
        snapshot.put("queued", queued);
        snapshot.put("active", active);
        return snapshot;
    }
}
