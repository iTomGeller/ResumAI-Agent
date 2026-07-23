package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import lombok.Data;

@Data
@TableName("run_memory_usage")
public class RunMemoryUsageRow {

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    @TableField("run_id")
    private String runId;

    @TableField("memory_id")
    private String memoryId;

    @TableField("consumer_agent")
    private String consumerAgent;

    @TableField("rank_no")
    private Integer rankNo;

    @TableField("vector_score")
    private BigDecimal vectorScore;

    @TableField("lexical_score")
    private BigDecimal lexicalScore;

    @TableField("recency_score")
    private BigDecimal recencyScore;

    @TableField("final_score")
    private BigDecimal finalScore;

    @TableField("decision")
    private String decision;

    @TableField("ignored_reason")
    private String ignoredReason;

    @TableField("create_time")
    private LocalDateTime createTime;
}
