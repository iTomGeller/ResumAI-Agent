package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("benchmark_run")
public class BenchmarkRunRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("benchmark_id")
    private String benchmarkId;

    @TableField("case_id")
    private String caseId;

    @TableField("policy_id")
    private String policyId;

    @TableField("run_id")
    private String runId;

    @TableField("status")
    private String status;

    @TableField("metrics")
    private String metrics;

    @TableField("report_path")
    private String reportPath;

    @TableField("started_at")
    private LocalDateTime startedAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;
}
