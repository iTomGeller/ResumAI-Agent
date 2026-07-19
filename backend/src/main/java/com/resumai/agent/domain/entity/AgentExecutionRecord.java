package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("agent_execution")
public class AgentExecutionRecord {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("run_id")
    private String runId;

    @TableField("agent_id")
    private String agentId;

    @TableField("status")
    private String status;

    @TableField("iterations")
    private Integer iterations;

    @TableField("llm_calls")
    private Integer llmCalls;

    @TableField("tool_calls")
    private Integer toolCalls;

    @TableField("output")
    private String output;

    @TableField("error_message")
    private String errorMessage;

    @TableField("started_at")
    private LocalDateTime startedAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;

    @TableField("create_time")
    private LocalDateTime createTime;
}
