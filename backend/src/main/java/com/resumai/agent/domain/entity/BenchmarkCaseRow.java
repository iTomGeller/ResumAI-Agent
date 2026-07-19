package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("benchmark_case")
public class BenchmarkCaseRow {

    @TableId(value = "case_id", type = IdType.INPUT)
    private String caseId;

    @TableField("dataset")
    private String dataset;

    @TableField("resume_text")
    private String resumeText;

    @TableField("jd_text")
    private String jdText;

    @TableField("user_question")
    private String userQuestion;

    @TableField("must_find")
    private String mustFind;

    @TableField("must_not_claim")
    private String mustNotClaim;

    @TableField("expected_evidence")
    private String expectedEvidence;

    @TableField("expected_risk")
    private String expectedRisk;

    @TableField("metadata")
    private String metadata;

    @TableField("create_time")
    private LocalDateTime createTime;
}
