package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_selection")
public class PolicySelectionRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("run_id")
    private String runId;

    @TableField("policy_id")
    private String policyId;

    @TableField("task_category")
    private String taskCategory;

    @TableField("selection_mode")
    private String selectionMode;

    @TableField("epsilon")
    private BigDecimal epsilon;

    @TableField("context")
    private String context;

    @TableField("create_time")
    private LocalDateTime createTime;
}
