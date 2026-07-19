---
name: project_depth_analysis
description: 兼容现有 ProjectEvalAgent 的项目证据评估 Skill。分析技术复杂度、个人贡献、业务结果和可验证性；新流程优先使用 ground-project-claims。
---

# Project Depth Analysis Compatibility

1. 将问题、行动、技术决策、个人贡献和结果拆为原子 claim。
2. 区分团队成果和个人 ownership。
3. 检查指标的基线、单位、时间窗与测量方式。
4. 不使用公司名气、公司规模或“大厂经历”作为项目质量分。
5. 不把“参与”自动判为低贡献，也不把“主导”直接当作已证明。
6. 无法核验时生成面试问题，不标记造假。

输出逐项目 `claims`、`assessment`、`sourceRefs`、`unknowns` 和 `clarifyingQuestions`。
