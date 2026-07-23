package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_promotion")
public class PolicyPromotion {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("experiment_id")
    private String experimentId;

    @TableField("candidate_id")
    private String candidateId;

    @TableField("run_type")
    private String runType;

    @TableField("cohort_key")
    private String cohortKey;

    @TableField("previous_policy_id")
    private String previousPolicyId;

    @TableField("promoted_policy_id")
    private String promotedPolicyId;

    @TableField("hard_gates_json")
    private String hardGatesJson;

    @TableField("metric_deltas_json")
    private String metricDeltasJson;

    @TableField("confidence_json")
    private String confidenceJson;

    @TableField("decision")
    private String decision;

    @TableField("decided_by")
    private String decidedBy;

    @TableField("decided_at")
    private LocalDateTime decidedAt;

    @TableField("reason")
    private String reason;
}
