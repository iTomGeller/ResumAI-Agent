package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.AgentRun;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface AgentRunMapper extends BaseMapper<AgentRun> {
}
