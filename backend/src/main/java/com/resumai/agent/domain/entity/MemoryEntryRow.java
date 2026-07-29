package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("memory_entry")
public class MemoryEntryRow {

    @TableId(value = "memory_id", type = IdType.INPUT)
    private String memoryId;

    @TableField("type")
    private String type;

    @TableField("owner_scope")
    private String ownerScope;

    @TableField("user_id")
    private String userId;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("run_id")
    private String runId;

    @TableField("content")
    private String content;

    @TableField("structured_content")
    private String structuredContent;

    @TableField("content_hash")
    private String contentHash;

    @TableField("source")
    private String source;

    @TableField("source_id")
    private String sourceId;

    @TableField("confidence")
    private BigDecimal confidence;

    @TableField("status")
    private String status;

    @TableField("version")
    private Integer version;

    @TableField("producer_version")
    private String producerVersion;

    @TableField("embedding")
    private String embedding;

    @TableField("sensitivity_level")
    private String sensitivityLevel;

    @TableField("expires_at")
    private LocalDateTime expiresAt;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;
}
