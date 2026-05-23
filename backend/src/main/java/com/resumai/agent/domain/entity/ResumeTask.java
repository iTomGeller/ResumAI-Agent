package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * 简历评估任务实体。
 *
 * <p>该实体承载一次完整简历评估任务的生命周期信息，包括文件地址、岗位类别、
 * 执行模式、任务状态和全局 TraceId。后续 OrchestratorAgent 会以该表作为任务编排入口。</p>
 */
@Data
@TableName("resume_task")
public class ResumeTask {

    /** 任务主键。 */
    @TableId
    private Long id;

    /** 简历文件地址。 */
    @TableField("file_url")
    private String fileUrl;

    /** 岗位唯一标识。 */
    @TableField("job_id")
    private String jobId;

    /** 岗位类别。 */
    @TableField("job_category")
    private String jobCategory;

    /** 执行模式：SERIAL/DAG_CONCURRENT。 */
    @TableField("execution_mode")
    private String executionMode;

    /** 任务状态。 */
    @TableField("status")
    private String status;

    /** 全局链路追踪 ID。 */
    @TableField("trace_id")
    private String traceId;

    /** 候选人姓名。 */
    @TableField("candidate_name")
    private String candidateName;

    /** 失败原因。 */
    @TableField("fail_reason")
    private String failReason;

    /** 任务开始时间。 */
    @TableField("start_time")
    private LocalDateTime startTime;

    /** 任务结束时间。 */
    @TableField("end_time")
    private LocalDateTime endTime;

    /** 创建时间。 */
    @TableField("create_time")
    private LocalDateTime createTime;

    /** 更新时间。 */
    @TableField("update_time")
    private LocalDateTime updateTime;

    /** 逻辑删除标记。 */
    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
