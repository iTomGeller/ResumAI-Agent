package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.RagasEvalMetrics;

/**
 * RAGAS 评估指标 Mapper，承担可信度评估结果的落库与查询职责。
 */
public interface RagasEvalMetricsMapper extends BaseMapper<RagasEvalMetrics> {
}
