package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("tool_call_log")
public class ToolCallLog {

    @TableId(value = "tool_call_id", type = IdType.INPUT)
    private String toolCallId;

    @TableField("run_id")
    private String runId;

    @TableField("agent_id")
    private String agentId;

    @TableField("tool_name")
    private String toolName;

    @TableField("arguments")
    private String arguments;

    @TableField("result_preview")
    private String resultPreview;

    @TableField("status")
    private String status;

    @TableField("error")
    private String error;

    @TableField("retry_count")
    private Integer retryCount;

    @TableField("duration_ms")
    private Long durationMs;

    @TableField("progress")
    private String progress;

    @TableField("heartbeat_at")
    private LocalDateTime heartbeatAt;

    @TableField("idempotency_key")
    private String idempotencyKey;

    @TableField("side_effect_level")
    private String sideEffectLevel;

    @TableField("started_at")
    private LocalDateTime startedAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;
}
