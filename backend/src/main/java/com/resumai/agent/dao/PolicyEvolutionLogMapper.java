package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.PolicyEvolutionLogRow;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface PolicyEvolutionLogMapper extends BaseMapper<PolicyEvolutionLogRow> {
}
