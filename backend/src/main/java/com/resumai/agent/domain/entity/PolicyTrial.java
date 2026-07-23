package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_trial")
public class PolicyTrial {

    @TableId("trial_id")
    private String trialId;

    @TableField("experiment_id")
    private String experimentId;

    @TableField("candidate_id")
    private String candidateId;

    @TableField("dataset_split")
    private String datasetSplit;

    @TableField("case_id")
    private String caseId;

    @TableField("repeat_no")
    private Integer repeatNo;

    @TableField("seed")
    private Long seed;

    @TableField("run_id")
    private String runId;

    @TableField("trajectory_uri")
    private String trajectoryUri;

    @TableField("result_uri")
    private String resultUri;

    @TableField("runner_sandbox_id")
    private String runnerSandboxId;

    @TableField("evaluator_sandbox_id")
    private String evaluatorSandboxId;

    @TableField("status")
    private String status;

    @TableField("total_reward")
    private BigDecimal totalReward;

    @TableField("cost_cny")
    private BigDecimal costCny;

    @TableField("latency_ms")
    private Integer latencyMs;

    @TableField("metrics_json")
    private String metricsJson;

    @TableField("reward_components_json")
    private String rewardComponentsJson;

    @TableField("error")
    private String error;

    @TableField("started_at")
    private LocalDateTime startedAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;

    @TableField("create_time")
    private LocalDateTime createTime;
}
