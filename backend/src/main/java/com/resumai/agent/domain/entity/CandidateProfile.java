package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

/** 候选人档案：一人一行，按 identity_key 去重。 */
@Data
@TableName("candidate_profile")
public class CandidateProfile {

    @TableId
    private Long id;

    @TableField("tenant_id")
    private String tenantId;

    @TableField("display_name")
    private String displayName;

    @TableField("email")
    private String email;

    @TableField("phone")
    private String phone;

    @TableField("identity_key")
    private String identityKey;

    @TableField("identity_source")
    private String identitySource;

    @TableField("resume_fingerprint")
    private String resumeFingerprint;

    @TableField("identity_confidence")
    private BigDecimal identityConfidence;

    @TableField("needs_merge_review")
    private Integer needsMergeReview;

    @TableField("data_origin")
    private String dataOrigin;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;

    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
