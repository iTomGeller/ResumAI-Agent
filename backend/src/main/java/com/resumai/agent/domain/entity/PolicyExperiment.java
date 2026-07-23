package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_experiment")
public class PolicyExperiment {

    @TableId("experiment_id")
    private String experimentId;

    @TableField("kind")
    private String kind;

    @TableField("status")
    private String status;

    @TableField("generation")
    private Integer generation;

    @TableField("champion_policy_id")
    private String championPolicyId;

    @TableField("train_dataset_hash")
    private String trainDatasetHash;

    @TableField("gate_dataset_hash")
    private String gateDatasetHash;

    @TableField("safety_dataset_hash")
    private String safetyDatasetHash;

    @TableField("code_sha")
    private String codeSha;

    @TableField("budget_cny")
    private BigDecimal budgetCny;

    @TableField("spent_cny")
    private BigDecimal spentCny;

    @TableField("created_by")
    private String createdBy;

    @TableField("started_at")
    private LocalDateTime startedAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;

    @TableField("error")
    private String error;

    @TableField("config_json")
    private String configJson;

    @TableField("eval_dataset")
    private String evalDataset;

    @TableField("gate_dataset")
    private String gateDataset;

    @TableField("safety_dataset")
    private String safetyDataset;

    @TableField("seeds_json")
    private String seedsJson;

    @TableField("repeats_per_case")
    private Integer repeatsPerCase;

    @TableField("case_limit")
    private Integer caseLimit;

    @TableField("run_type")
    private String runType;

    @TableField("cohort_key")
    private String cohortKey;

    @TableField("base_policy_id")
    private String basePolicyId;

    @TableField("progress_pct")
    private BigDecimal progressPct;

    @TableField("progress_phase")
    private String progressPhase;

    @TableField("pause_requested")
    private Integer pauseRequested;

    @TableField("cancel_requested")
    private Integer cancelRequested;

    @TableField("auto_promote")
    private Integer autoPromote;

    @TableField("runner_image_digest")
    private String runnerImageDigest;

    @TableField("evaluator_image_digest")
    private String evaluatorImageDigest;

    @TableField("result_json")
    private String resultJson;

    @TableField("note")
    private String note;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;
}
