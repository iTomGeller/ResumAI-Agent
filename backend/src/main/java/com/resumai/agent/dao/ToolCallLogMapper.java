package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.ToolCallLog;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface ToolCallLogMapper extends BaseMapper<ToolCallLog> {
}
