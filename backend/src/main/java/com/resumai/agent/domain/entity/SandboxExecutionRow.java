package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("sandbox_execution")
public class SandboxExecutionRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("sandbox_id")
    private String sandboxId;

    @TableField("run_id")
    private String runId;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("tool_name")
    private String toolName;

    @TableField("container_id")
    private String containerId;

    @TableField("status")
    private String status;

    /** POLICY_EVOLUTION / BENCHMARK / REPLAY / LEGACY_CANDIDATE_EVALUATION */
    @TableField("purpose")
    private String purpose;

    @TableField("experiment_id")
    private String experimentId;

    @TableField("trial_id")
    private String trialId;

    @TableField("exit_code")
    private Integer exitCode;

    @TableField("duration_ms")
    private Long durationMs;

    @TableField("stdout_tail")
    private String stdoutTail;

    @TableField("stderr_tail")
    private String stderrTail;

    @TableField("error")
    private String error;

    @TableField("expire_at")
    private LocalDateTime expireAt;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("finished_at")
    private LocalDateTime finishedAt;
}
