package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.AgentExecutionTrace;

/**
 * Agent 执行链路 Mapper，承担 TraceController 与 SSE 推送链路的持久化职责。
 */
public interface AgentExecutionTraceMapper extends BaseMapper<AgentExecutionTrace> {
}
