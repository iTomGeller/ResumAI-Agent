package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.*;
import java.time.LocalDateTime;

@TableName("jd_library")
public class JdLibrary {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String jdId;
    private String title;
    private String category;
    private String description;
    private LocalDateTime createTime;
    private LocalDateTime updateTime;

    @TableLogic
    private Integer deleted;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getJdId() { return jdId; }
    public void setJdId(String jdId) { this.jdId = jdId; }
    public String getTitle() { return title; }
    public void setTitle(String title) { this.title = title; }
    public String getCategory() { return category; }
    public void setCategory(String category) { this.category = category; }
    public String getDescription() { return description; }
    public void setDescription(String description) { this.description = description; }
    public LocalDateTime getCreateTime() { return createTime; }
    public void setCreateTime(LocalDateTime createTime) { this.createTime = createTime; }
    public LocalDateTime getUpdateTime() { return updateTime; }
    public void setUpdateTime(LocalDateTime updateTime) { this.updateTime = updateTime; }
    public Integer getDeleted() { return deleted; }
    public void setDeleted(Integer deleted) { this.deleted = deleted; }
}
