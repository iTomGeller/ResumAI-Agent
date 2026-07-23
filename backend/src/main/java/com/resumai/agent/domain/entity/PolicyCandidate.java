package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_candidate")
public class PolicyCandidate {

    @TableId("candidate_id")
    private String candidateId;

    @TableField("experiment_id")
    private String experimentId;

    @TableField("parent_policy_id")
    private String parentPolicyId;

    @TableField("bundle_policy_id")
    private String bundlePolicyId;

    @TableField("config_json")
    private String configJson;

    @TableField("config_hash")
    private String configHash;

    @TableField("mutation_patch")
    private String mutationPatch;

    @TableField("reflector_model")
    private String reflectorModel;

    @TableField("mutation_reason")
    private String mutationReason;

    @TableField("gate_metrics_json")
    private String gateMetricsJson;

    @TableField("status")
    private String status;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;
}
