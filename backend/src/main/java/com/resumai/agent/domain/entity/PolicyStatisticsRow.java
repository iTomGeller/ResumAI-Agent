package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_statistics")
public class PolicyStatisticsRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("policy_id")
    private String policyId;

    @TableField("task_category")
    private String taskCategory;

    @TableField("run_count")
    private Integer runCount;

    @TableField("reward_count")
    private Integer rewardCount;

    @TableField("total_reward")
    private BigDecimal totalReward;

    @TableField("avg_reward")
    private BigDecimal avgReward;

    @TableField("reward_sq_sum")
    private BigDecimal rewardSqSum;

    @TableField("success_count")
    private Integer successCount;

    @TableField("failure_count")
    private Integer failureCount;

    @TableField("update_time")
    private LocalDateTime updateTime;
}
