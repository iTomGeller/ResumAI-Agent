package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_experiment_event")
public class PolicyExperimentEvent {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("experiment_id")
    private String experimentId;

    @TableField("seq")
    private Integer seq;

    @TableField("event_type")
    private String eventType;

    @TableField("payload")
    private String payload;

    @TableField("create_time")
    private LocalDateTime createTime;
}
