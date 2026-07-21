package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

/** 策略进化审计：每次生成候选 / 晋升 / 淘汰都落一行，谱系可回放。 */
@Data
@TableName("policy_evolution_log")
public class PolicyEvolutionLogRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("generation")
    private Integer generation;

    @TableField("policy_id")
    private String policyId;

    @TableField("parent_policy_id")
    private String parentPolicyId;

    /** CANDIDATE_CREATED / PROMOTED / RETIRED / REJECTED */
    @TableField("action")
    private String action;

    @TableField("mutation_reason")
    private String mutationReason;

    @TableField("benchmark_score")
    private BigDecimal benchmarkScore;

    @TableField("champion_score")
    private BigDecimal championScore;

    @TableField("detail")
    private String detail;

    @TableField("create_time")
    private LocalDateTime createTime;
}
