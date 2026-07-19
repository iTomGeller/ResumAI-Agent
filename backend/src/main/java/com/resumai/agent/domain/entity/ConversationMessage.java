package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("conversation_message")
public class ConversationMessage {

    @TableId(type = IdType.AUTO)
    private Long id;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("client_message_id")
    private String clientMessageId;

    private String role;

    @TableField("intent_type")
    private String intentType;

    private String content;

    @TableField("revision_no")
    private Integer revisionNo;

    @TableField("metadata_json")
    private String metadataJson;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableLogic
    @TableField("deleted")
    private Integer deleted;
}
