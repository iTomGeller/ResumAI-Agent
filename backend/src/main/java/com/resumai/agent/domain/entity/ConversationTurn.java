package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("conversation_turn")
public class ConversationTurn {

    @TableId(value = "turn_id", type = IdType.INPUT)
    private String turnId;

    @TableField("conversation_id")
    private String conversationId;

    @TableField("client_message_id")
    private String clientMessageId;

    private String disposition;

    private String intent;

    /** PENDING / STREAMING / COMPLETED / FAILED */
    private String status;

    private String content;

    private String answer;

    private String citations;

    private String actions;

    private String error;

    @TableField("created_at")
    private LocalDateTime createdAt;

    @TableField("finished_at")
    private LocalDateTime finishedAt;
}
