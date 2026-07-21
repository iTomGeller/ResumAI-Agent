package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("policy_bundle")
public class PolicyBundleRow {

    @TableId(value = "policy_id", type = IdType.INPUT)
    private String policyId;

    @TableField("name")
    private String name;

    @TableField("description")
    private String description;

    @TableField("config")
    private String config;

    @TableField("status")
    private String status;

    @TableField("is_champion")
    private Integer isChampion;

    /** 变异来源策略（进化谱系）。 */
    @TableField("parent_policy_id")
    private String parentPolicyId;

    /** 进化代数，0 = 人工种子策略。 */
    @TableField("generation")
    private Integer generation;

    @TableField("version")
    private Integer version;

    @TableField("create_time")
    private LocalDateTime createTime;

    @TableField("update_time")
    private LocalDateTime updateTime;
}
