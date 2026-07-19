package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.PolicyStatisticsRow;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface PolicyStatisticsMapper extends BaseMapper<PolicyStatisticsRow> {
}
