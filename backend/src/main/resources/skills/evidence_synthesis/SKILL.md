---
name: evidence_synthesis
description: 兼容现有 report graph 的证据融合 Skill。汇总简历、JD、RAG 和真实成功返回的外部工具证据，并区分已支持、冲突与未检查；新流程优先使用 calibrate-evidence-confidence。
---

# Evidence Synthesis Compatibility

按 claim 融合现有输入，并遵守以下规则：

1. 简历和用户补充属于候选人自述，不是独立验证。
2. RAG 只定位其原始文档，不作为第二个独立来源。
3. 只使用调用成功、带来源引用的外部工具结果。
4. 未调用、不可用或失败的 GitHub、博客、StackOverflow、图谱等来源标为 `not_checked`，不得生成内容或加入权重。
5. 不使用固定来源权重，不因外部结果正面而机械加分。
6. 缺少证据表示未知，不等于 claim 为假或候选人存在风险。

输出 `claims`、`conflicts`、`unknowns`、`sourceRefs`、`toolHealth` 和 `recommendationBasis`。每个结论必须能回溯到输入 source ref。
