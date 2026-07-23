package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import lombok.Data;

@Data
@TableName("policy_metric")
public class PolicyMetric {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("trial_id")
    private String trialId;

    @TableField("metric_name")
    private String metricName;

    @TableField("metric_value")
    private BigDecimal metricValue;

    @TableField("metric_status")
    private String metricStatus;

    @TableField("detail_json")
    private String detailJson;
}
