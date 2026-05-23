package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * 系统宏观调度规则实体。
 *
 * <p>该实体定义不同岗位类别所需的 Agent 拓扑、首选 RAG 策略、检索 TopK、
 * 最大重试次数与 RAGAS 阈值，是 OrchestratorAgent 动态派生子 Agent 的规则来源。</p>
 */
@Data
@TableName("system_orchestration_rule")
public class SystemOrchestrationRule {

    /** 主键。 */
    @TableId
    private Long id;

    /** 岗位类别。 */
    @TableField("job_category")
    private String jobCategory;

    /** 必需 Agent 列表，JSON 字符串。 */
    @TableField("required_agents")
    private String requiredAgents;

    /** 首选 RAG 策略。 */
    @TableField("preferred_rag_strategy")
    private String preferredRagStrategy;

    /** 检索 TopK。 */
    @TableField("top_k")
    private Integer topK;

    /** 最大重试次数。 */
    @TableField("max_retry")
    private Integer maxRetry;

    /** 事实一致性阈值。 */
    @TableField("faithfulness_threshold")
    private BigDecimal faithfulnessThreshold;

    /** 执行策略。 */
    @TableField("execution_policy")
    private String executionPolicy;

    /** 是否启用。 */
    @TableField("enabled")
    private Integer enabled;

    /** 规则版本。 */
    @TableField("version")
    private Integer version;

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
