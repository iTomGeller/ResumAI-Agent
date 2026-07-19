package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("run_event")
public class RunEvent {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("run_id")
    private String runId;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("trace_id")
    private String traceId;

    @TableField("seq")
    private Integer seq;

    @TableField("event_type")
    private String eventType;

    @TableField("agent_id")
    private String agentId;

    @TableField("tool_name")
    private String toolName;

    @TableField("payload")
    private String payload;

    @TableField("create_time")
    private LocalDateTime createTime;
}
