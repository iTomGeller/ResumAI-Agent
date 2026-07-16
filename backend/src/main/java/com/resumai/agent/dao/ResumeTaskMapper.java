package com.resumai.agent.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.resumai.agent.domain.entity.ResumeTask;

/**
 * 简历评估任务 Mapper。
 *
 * <p>承担 OrchestratorAgent 与 ResumeEvaluationService 对 resume_task 的 CRUD 落库职责。</p>
 */
public interface ResumeTaskMapper extends BaseMapper<ResumeTask> {
}
