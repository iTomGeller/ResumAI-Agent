package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("agent_run")
public class AgentRun {

    @TableId(value = "run_id", type = IdType.INPUT)
    private String runId;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("user_id")
    private String userId;

    @TableField("trace_id")
    private String traceId;

    @TableField("revision_no")
    private Integer revisionNo;

    @TableField("run_type")
    private String runType;

    @TableField("queue_mode")
    private String queueMode;

    /** 1 = pause after the Coordinator plan and wait for user approval. */
    @TableField("plan_mode")
    private Integer planMode;

    @TableField("user_message")
    private String userMessage;

    @TableField("merged_message_ids")
    private String mergedMessageIds;

    @TableField("status")
    private String status;

    @TableField("current_agent")
    private String currentAgent;

    @TableField("current_tool")
    private String currentTool;

    @TableField("current_phase")
    private String currentPhase;

    @TableField("answer")
    private String answer;

    @TableField("shared_state")
    private String sharedState;

    @TableField("metrics")
    private String metrics;

    @TableField("prompt_versions")
    private String promptVersions;

    @TableField("skill_versions")
    private String skillVersions;

    @TableField("retry_count")
    private Integer retryCount;

    @TableField("error_code")
    private String errorCode;

    @TableField("error_message")
    private String errorMessage;

    @TableField("cancellation_reason")
    private String cancellationReason;

    @TableField("pause_reason")
    private String pauseReason;

    /** RunExecutionSnapshot JSON captured at the pause boundary. */
    @TableField("execution_snapshot")
    private String executionSnapshot;

    /** Set when this run mirrors a legacy resume_task evaluation. */
    @TableField("source_task_trace_id")
    private String sourceTaskTraceId;

    @TableField("conv_permit_id")
    private String convPermitId;

    @TableField("global_permit_id")
    private String globalPermitId;

    @TableField("created_at")
    private LocalDateTime createdAt;

    @TableField("started_at")
    private LocalDateTime startedAt;

    @TableField("updated_at")
    private LocalDateTime updatedAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;

    @TableField("timeout_at")
    private LocalDateTime timeoutAt;

    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
