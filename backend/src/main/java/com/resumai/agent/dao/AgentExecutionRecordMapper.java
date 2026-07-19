package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.AgentExecutionRecord;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface AgentExecutionRecordMapper extends BaseMapper<AgentExecutionRecord> {
}
