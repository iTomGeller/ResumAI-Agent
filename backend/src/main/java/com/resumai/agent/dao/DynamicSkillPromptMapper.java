package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.DynamicSkillPrompt;

/**
 * 动态 Skill Prompt Mapper，承担 Skill Prompt 版本化读写职责。
 */
public interface DynamicSkillPromptMapper extends BaseMapper<DynamicSkillPrompt> {
}
