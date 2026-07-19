package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * 人工反馈日志实体。
 *
 * <p>记录 HR 或面试官对 AI 报告的点赞、点踩、批注、修正和推荐覆盖动作，
 * 是 RLHF 闭环和 Meta-Agent 进化分析的重要输入。</p>
 */
@Data
@TableName("human_feedback_log")
public class HumanFeedbackLog {

    /** 主键。 */
    @TableId
    private Long id;

    /** 全局链路追踪 ID。 */
    @TableField("trace_id")
    private String traceId;

    /** 关联的对话 Run。 */
    @TableField("run_id")
    private String runId;

    /** Run 使用的策略。 */
    @TableField("policy_id")
    private String policyId;

    /** 结构化反馈载荷（分数修正、遗漏证据等）。 */
    @TableField("structured_payload")
    private String structuredPayload;

    /** 报告 ID。 */
    @TableField("report_id")
    private String reportId;

    /** 人工评分。 */
    @TableField("rating_score")
    private Integer ratingScore;

    /** 人工批注。 */
    @TableField("human_comment")
    private String humanComment;

    /** 修正动作。 */
    @TableField("fix_action")
    private String fixAction;

    /** 反馈类型。 */
    @TableField("feedback_type")
    private String feedbackType;

    /** 反馈人。 */
    @TableField("reviewer")
    private String reviewer;

    /** 是否采纳。 */
    @TableField("adopted")
    private Integer adopted;

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
