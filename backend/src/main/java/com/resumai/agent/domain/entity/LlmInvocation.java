package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("llm_invocation")
public class LlmInvocation {

    @TableId
    private String id;

    @TableField("trace_id")
    private String traceId;

    @TableField("span_id")
    private String spanId;

    @TableField("model_name")
    private String modelName;

    @TableField("agent_role")
    private String agentRole;

    @TableField("purpose")
    private String purpose;

    @TableField("request_started_at")
    private LocalDateTime requestStartedAt;

    @TableField("duration_ms")
    private Long durationMs;

    @TableField("input_tokens")
    private Integer inputTokens;

    @TableField("output_tokens")
    private Integer outputTokens;

    @TableField("finish_reason")
    private String finishReason;

    @TableField("truncated")
    private Integer truncated;

    @TableField("prompt_chars")
    private Integer promptChars;

    @TableField("response_chars")
    private Integer responseChars;

    @TableField("prompt_preview")
    private String promptPreview;

    @TableField("response_preview")
    private String responsePreview;

    @TableField("prompt_full")
    private String promptFull;

    @TableField("response_full")
    private String responseFull;

    @TableField("prompt_object_key")
    private String promptObjectKey;

    @TableField("response_object_key")
    private String responseObjectKey;

    @TableField("error_code")
    private String errorCode;

    @TableField("error_body")
    private String errorBody;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;

    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
