package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * Agent 执行链路实体。
 *
 * <p>每一条记录对应一次 Controller、Service、Agent、Skill、RAG、MCP 或 LLM
 * 调用产生的 Span，用于前端瀑布流展示、审计、故障定位和 Meta-Agent 反思。</p>
 */
@Data
@TableName("agent_execution_trace")
public class AgentExecutionTrace {

    /** 主键。 */
    @TableId
    private Long id;

    /** 全局链路追踪 ID。 */
    @TableField("trace_id")
    private String traceId;

    /** 当前 Span ID。 */
    @TableField("span_id")
    private String spanId;

    /** 父 Span ID。 */
    @TableField("parent_span_id")
    private String parentSpanId;

    /** Agent 角色。 */
    @TableField("agent_role")
    private String agentRole;

    /** 挂载 Skill 名称。 */
    @TableField("skill_name")
    private String skillName;

    /** 工具调用名称。 */
    @TableField("tool_call")
    private String toolCall;

    /** RAG 策略。 */
    @TableField("rag_strategy")
    private String ragStrategy;

    /** 模型名称。 */
    @TableField("model_name")
    private String modelName;

    /** 输入摘要。 */
    @TableField("input_summary")
    private String inputSummary;

    /** 输出摘要。 */
    @TableField("output_summary")
    private String outputSummary;

    /** 结构化调用载荷，JSON 字符串。 */
    @TableField("payload")
    private String payload;

    /** 耗时毫秒。 */
    @TableField("duration_ms")
    private Long durationMs;

    /** Token 成本。 */
    @TableField("cost_tokens")
    private Long costTokens;

    /** 重试次数。 */
    @TableField("retry_count")
    private Integer retryCount;

    /** 执行状态。 */
    @TableField("status")
    private String status;

    /** 异常信息。 */
    @TableField("error_message")
    private String errorMessage;

    /** 创建时间。 */
    @TableField("create_time")
    private LocalDateTime createTime;

    /** 更新时间。 */
    @TableField("update_time")
    private LocalDateTime updateTime;

    /** 逻辑删除标记。 */
    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
