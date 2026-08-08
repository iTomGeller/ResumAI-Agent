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
 * 执行模式、任务状态和全局 TraceId。后续 CoordinatorAgent 会以该表作为任务编排入口。</p>
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

    /** 队列调度状态。 */
    @TableField("queue_status")
    private String queueStatus;

    /** 上传 HR 标识。 */
    @TableField("uploaded_by")
    private String uploadedBy;

    /** 租户标识。 */
    @TableField("tenant_id")
    private String tenantId;

    /** 任务优先级，越大越优先。 */
    @TableField("priority")
    private Integer priority;

    /** 入队时间。 */
    @TableField("queued_at")
    private LocalDateTime queuedAt;

    /** 开始消费时间。 */
    @TableField("started_at")
    private LocalDateTime startedAt;

    /** 结束时间。 */
    @TableField("finished_at")
    private LocalDateTime finishedAt;

    /** 已重试次数。 */
    @TableField("attempt_count")
    private Integer attemptCount;

    /** 下次重试时间。 */
    @TableField("next_retry_at")
    private LocalDateTime nextRetryAt;

    /** 消费 worker 标识。 */
    @TableField("worker_id")
    private String workerId;

    /** 全局链路追踪 ID。 */
    @TableField("trace_id")
    private String traceId;

    /** 持续对话 ID；同一会话可包含多个不可变评估 revision。 */
    @TableField("conversation_id")
    private String conversationId;

    /** 会话内的评估版本号，从 1 开始递增。 */
    @TableField("revision_no")
    private Integer revisionNo;

    /** Python runtime 的运行 ID，用于拒绝迟到或串线回调。 */
    @TableField("workflow_run_id")
    private String workflowRunId;

    @TableField("base_workflow_run_id")
    private String baseWorkflowRunId;

    @TableField("supersedes_trace_id")
    private String supersedesTraceId;

    @TableField("superseded_by_trace_id")
    private String supersededByTraceId;

    /** 进程恢复和 revision 重建所需的不可变输入快照。 */
    @TableField("resume_text")
    private String resumeText;

    @TableField("job_description")
    private String jobDescription;

    @TableField("evaluation_brief")
    private String evaluationBrief;

    @TableField("invalidated_nodes")
    private String invalidatedNodes;

    @TableField("rag_options")
    private String ragOptions;

    /** 候选人姓名。 */
    @TableField("candidate_name")
    private String candidateName;

    /** 关联候选人档案。 */
    @TableField("candidate_id")
    private Long candidateId;

    /** 关联投递申请。 */
    @TableField("application_id")
    private Long applicationId;

    /** 数据来源：USER_UPLOAD/BENCHMARK/ACCEPTANCE/SYSTEM。 */
    @TableField("data_origin")
    private String dataOrigin;

    /** 候选人关联状态：LINKED/SKIPPED/FAILED/PENDING。 */
    @TableField("candidate_link_status")
    private String candidateLinkStatus;

    /** 关联/跳过原因。 */
    @TableField("candidate_link_reason")
    private String candidateLinkReason;

    /** 简历文件名（列表展示）。 */
    @TableField("file_name")
    private String fileName;

    /** 综合评分（列表展示）。 */
    @TableField("overall_score")
    private Integer overallScore;

    /** 推荐结论（列表展示）。 */
    @TableField("recommendation")
    private String recommendation;

    /** 匹配岗位标题（列表展示）。 */
    @TableField("matched_jd_title")
    private String matchedJdTitle;

    /** JD 匹配分（列表展示）。 */
    @TableField("jd_match_score")
    private Double jdMatchScore;

    /** 评估耗时毫秒（列表展示）。 */
    @TableField("duration_ms")
    private Long durationMs;

    /** Token 成本（列表展示）。 */
    @TableField("token_cost")
    private Integer tokenCost;

    /** 评估摘要（列表展示）。 */
    @TableField("summary")
    private String summary;

    /** 简历对象存储 key。 */
    @TableField("resume_object_key")
    private String resumeObjectKey;

    /** 失败原因。 */
    @TableField("fail_reason")
    private String failReason;

    /** 评估结果快照 JSON。 */
    @TableField("result_payload")
    private String resultPayload;

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
