package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * RAGAS 评估指标实体。
 *
 * <p>用于记录每个 Agent 输出的上下文精确率、召回率、事实一致性和回答相关性，
 * 为低可信输出重试、幻觉治理和 Meta-Agent 策略优化提供依据。</p>
 */
@Data
@TableName("ragas_eval_metrics")
public class RagasEvalMetrics {

    /** 主键。 */
    @TableId
    private Long id;

    /** 全局链路追踪 ID。 */
    @TableField("trace_id")
    private String traceId;

    /** 被评估 Span ID。 */
    @TableField("span_id")
    private String spanId;

    /** 上下文精确率。 */
    @TableField("context_precision")
    private BigDecimal contextPrecision;

    /** 上下文召回率。 */
    @TableField("context_recall")
    private BigDecimal contextRecall;

    /** 事实一致性。 */
    @TableField("faithfulness")
    private BigDecimal faithfulness;

    /** 回答相关性。 */
    @TableField("answer_relevancy")
    private BigDecimal answerRelevancy;

    /** 综合评分。 */
    @TableField("overall_score")
    private BigDecimal overallScore;

    /** 是否通过阈值。 */
    @TableField("passed")
    private Integer passed;

    /** 裁判原因。 */
    @TableField("judge_reason")
    private String judgeReason;

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
