package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("context_snapshot")
public class ContextSnapshotRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("run_id")
    private String runId;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("summary_version")
    private Integer summaryVersion;

    @TableField("source_message_start_id")
    private Long sourceMessageStartId;

    @TableField("source_message_end_id")
    private Long sourceMessageEndId;

    @TableField("first_kept_message_id")
    private Long firstKeptMessageId;

    @TableField("before_token_estimate")
    private Integer beforeTokenEstimate;

    @TableField("after_token_estimate")
    private Integer afterTokenEstimate;

    @TableField("reason")
    private String reason;

    @TableField("summary")
    private String summary;

    @TableField("create_time")
    private LocalDateTime createTime;
}
