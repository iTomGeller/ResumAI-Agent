package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.HumanFeedbackLog;

/**
 * 人工反馈日志 Mapper，承担 FeedbackController 的 RLHF 数据落库职责。
 */
public interface HumanFeedbackLogMapper extends BaseMapper<HumanFeedbackLog> {
}
