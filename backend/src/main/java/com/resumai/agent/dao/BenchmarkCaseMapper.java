package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.BenchmarkCaseRow;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface BenchmarkCaseMapper extends BaseMapper<BenchmarkCaseRow> {
}
