package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.SystemOrchestrationRule;

/**
 * 系统宏观调度规则 Mapper，承担 CoordinatorAgent 规划执行 Agent 时所需的规则查询职责。
 */
public interface SystemOrchestrationRuleMapper extends BaseMapper<SystemOrchestrationRule> {
}
