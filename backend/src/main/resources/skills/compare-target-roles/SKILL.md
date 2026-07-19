---
name: compare-target-roles
description: 使用候选人证据和明确偏好比较两到五个目标岗位或 JD。用户选择秋招方向、比较 offer 前岗位、调整投递优先级或询问更适合哪个岗位时使用。
---

# Compare Target Roles

## 输入

接收 2–5 个 `normalizedJd`、候选人 evidence ledger，以及可选的地点、方向、成长、稳定性等用户偏好和权重。

## 流程

1. 对齐各岗位的 requirement，避免只比较职位名称。
2. 分别计算简历证据覆盖、需补充证明和真实技能差距。
3. 将“能力匹配”和“用户偏好匹配”分成两列。
4. 没有偏好时展示 trade-off，不强行输出唯一最佳岗位。
5. 权重会改变结论时给出简短敏感性说明。

## 输出

```json
{
  "roles": [{"roleId": "", "evidenceCoverage": 0, "preferenceFit": null, "strengths": [], "gaps": [], "unknowns": []}],
  "tradeOffs": [],
  "recommendation": null,
  "recommendationBasis": [],
  "sensitivity": []
}
```

## 证据边界

- 简历未写标为 `no_resume_evidence`，不等于候选人不会。
- 岗位是否仍开放、薪资和地点只有实时来源成功返回时才陈述，并附时间。
- 不用公司名气替代岗位质量；不替用户虚构偏好。
- 不跨不同口径直接比较未经解释的总分。
