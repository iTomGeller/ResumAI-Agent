package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("conversation_session")
public class ConversationSession {

    @TableId
    private String id;

    @TableField("user_id")
    private String userId;

    @TableField("title")
    private String title;

    @TableField("resume_text")
    private String resumeText;

    @TableField("job_description")
    private String jobDescription;

    @TableField("job_category")
    private String jobCategory;

    @TableField("summary")
    private String summary;

    @TableField("summary_version")
    private Integer summaryVersion;

    @TableField("current_goal")
    private String currentGoal;

    @TableField("active_trace_id")
    private String activeTraceId;

    @TableField("active_revision")
    private Integer activeRevision;

    @TableField("tenant_id")
    private String tenantId;

    @TableField("created_by")
    private String createdBy;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;

    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
