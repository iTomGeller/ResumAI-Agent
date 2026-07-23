package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_champion_assignment")
public class PolicyChampionAssignmentRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("run_type")
    private String runType;

    @TableField("cohort_key")
    private String cohortKey;

    @TableField("policy_id")
    private String policyId;

    @TableField("experiment_id")
    private String experimentId;

    @TableField("approved_by")
    private String approvedBy;

    @TableField("approved_at")
    private LocalDateTime approvedAt;

    @TableField("version")
    private Integer version;

    @TableField("active")
    private Integer active;
}
