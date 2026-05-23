package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.MetaEvolutionHistory;

/**
 * Meta-Agent 自进化历史 Mapper，记录 Prompt/调度规则/RAG 策略的版本化变更。
 */
public interface MetaEvolutionHistoryMapper extends BaseMapper<MetaEvolutionHistory> {
}
