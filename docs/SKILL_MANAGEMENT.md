# Skill 管理

SkillManager：`workflow/app/runtime/skills.py`

内置 Skill：
resume_parsing, jd_requirement_analysis, java_backend_evaluation,
ai_agent_job_evaluation, project_depth_analysis, timeline_risk_analysis,
evidence_verification, resume_rewrite, interview_question_generation,
report_generation

按任务动态加载，禁止一次塞入全部 Skill。参与 Agent 选择、Prompt 组装、Trace、Policy、Benchmark。
