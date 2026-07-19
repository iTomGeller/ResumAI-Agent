package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.PolicySelectionRow;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface PolicySelectionMapper extends BaseMapper<PolicySelectionRow> {
}
