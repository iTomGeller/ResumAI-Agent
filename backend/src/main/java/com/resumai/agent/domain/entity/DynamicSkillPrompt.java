package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/**
 * 动态 Skill Prompt 实体。
 *
 * <p>用于保存不同 Skill 的 Prompt 模板和版本，避免将 Prompt 写死在代码中，
 * 也为 Meta-Agent 后续自动优化 Prompt 提供版本化落点。</p>
 */
@Data
@TableName("dynamic_skill_prompt")
public class DynamicSkillPrompt {

    /** 主键。 */
    @TableId
    private Long id;

    /** Skill 名称。 */
    @TableField("skill_name")
    private String skillName;

    /** Prompt 模板。 */
    @TableField("prompt_template")
    private String promptTemplate;

    /** Prompt 版本。 */
    @TableField("version")
    private Integer version;

    /** 是否启用。 */
    @TableField("enabled")
    private Integer enabled;

    /** 说明。 */
    @TableField("description")
    private String description;

    /** 创建人。 */
    @TableField("created_by")
    private String createdBy;

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
