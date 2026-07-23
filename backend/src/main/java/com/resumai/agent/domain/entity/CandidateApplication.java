package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/** 候选人投递/申请：可关联多次评估任务。 */
@Data
@TableName("candidate_application")
public class CandidateApplication {

    @TableId
    private Long id;

    @TableField("candidate_id")
    private Long candidateId;

    @TableField("tenant_id")
    private String tenantId;

    @TableField("job_category")
    private String jobCategory;

    @TableField("job_id")
    private String jobId;

    /** 租户内复用键：candidateId:normalizedJob */
    @TableField("application_key")
    private String applicationKey;

    @TableField("stage")
    private String stage;

    @TableField("owner_hr_id")
    private String ownerHrId;

    @TableField("latest_task_id")
    private Long latestTaskId;

    @TableField("latest_trace_id")
    private String latestTraceId;

    @TableField("latest_score")
    private Integer latestScore;

    @TableField("latest_recommendation")
    private String latestRecommendation;

    @TableField("source_file_name")
    private String sourceFileName;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;

    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
