package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.ConversationTurn;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface ConversationTurnMapper extends BaseMapper<ConversationTurn> {
}
