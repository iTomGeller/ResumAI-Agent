package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * Meta-Agent 自进化历史实体。
 *
 * <p>记录 MetaReflectionAgent 对 Prompt、调度规则、RAG 策略、Agent 拓扑和阈值的修改建议或自动变更，
 * 每条记录都保留修改前后内容、风险等级、审核状态和回滚数据。</p>
 */
@Data
@TableName("meta_evolution_history")
public class MetaEvolutionHistory {

    /** 主键。 */
    @TableId
    private Long id;

    /** 触发进化的 Trace ID。 */
    @TableField("trace_id")
    private String traceId;

    /** 进化类型。 */
    @TableField("evolution_type")
    private String evolutionType;

    /** 目标表。 */
    @TableField("target_table")
    private String targetTable;

    /** 目标记录 ID。 */
    @TableField("target_id")
    private Long targetId;

    /** 修改前值，JSON 字符串。 */
    @TableField("before_value")
    private String beforeValue;

    /** 修改后值，JSON 字符串。 */
    @TableField("after_value")
    private String afterValue;

    /** 修改原因。 */
    @TableField("reason")
    private String reason;

    /** 风险等级。 */
    @TableField("risk_level")
    private String riskLevel;

    /** 审核状态。 */
    @TableField("approval_status")
    private String approvalStatus;

    /** 回滚数据，JSON 字符串。 */
    @TableField("rollback_data")
    private String rollbackData;

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
