---
name: tech_stack_assessment
description: 兼容现有 TechEvalAgent 的技术证据评估 Skill。根据具体 JD 评估技术主张和深度；新流程优先使用 assess-technical-evidence。
---

# Tech Stack Assessment Compatibility

1. 从 JD requirement 派生维度，不对所有岗位固定评估分布式系统或 AI/ML。
2. 为每个维度引用简历或项目 source ref。
3. 区分提及、使用、设计、运维和排障深度。
4. 外部资料只有工具调用成功且身份关联明确时使用。
5. 缺少证据标为 `not_checked` 或 `no_resume_evidence`，不推断能力不存在。

输出 `dimensions`、`overallTechScore`、`scoreBasis`、`gaps`、`unknowns` 和 `toolHealth`。
