---
name: audit-job-relevant-evaluation
description: 审计简历评分、风险判断和报告理由是否与目标岗位直接相关，并识别受保护属性或社会经济代理变量。生成风险结论、最终评分或招聘建议前后使用。
---

# Audit Job-Relevant Evaluation

只保留能够连接到明确 JD 要求和候选人证据的评价理由。需要逐项检查时读取 `references/job-relevance-checklist.md`。

## 输入

- `normalizedJd`：带原文引用的岗位要求。
- `candidateEvidence`：去标识化后的 claim 与证据。
- `rubric`：每个维度的岗位相关性和评分规则。
- `scoresAndReasons`：各 Agent 的分数、风险及理由。
- `jurisdictionNotes`：如有，仅作为人工复核提示。

## 审计步骤

1. 为每条评分理由寻找对应 JD requirement ID。
2. 为每条理由寻找候选人 source ref。
3. 检查是否使用受保护属性或代理变量。
4. 区分事实不一致、证据缺失和与岗位无关的信息。
5. 删除无岗位依据的扣分，重写带偏见或带定罪意味的表述。
6. 输出需要人工合规确认的条件，不自行给法律结论。

## 默认禁止作为负面信号

- 姓名、照片、年龄、性别、民族、国籍、婚育、宗教、健康和家庭信息。
- 毕业年份作为年龄代理。
- 学校或前雇主名气、公司规模、地域出身作为能力代理。
- 非全日制、专升本、空档期、跳槽次数本身。
- GitHub star 数、社交影响力或是否经营技术博客本身。

只有与岗位直接相关、在 JD 中明确且适用性已经人工确认的条件才能进入 rubric。

## 输出

```json
{
  "approvedReasons": [{"reasonId": "r1", "requirementId": "jd-4", "sourceRefs": ["resume:p1:l12"]}],
  "removedReasons": [{"reasonId": "r2", "category": "prestige_proxy", "reason": "公司名气不是 JD 能力要求"}],
  "proxyFlags": [],
  "recalculationRequired": true,
  "humanReview": [],
  "unknowns": []
}
```

## 边界

- 不推断缺失的受保护属性。
- 不把合规审计写成候选人风险。
- 不提供法律意见；法规或特殊岗位条件交人工确认。
- 不为了“去偏见”删除确有岗位依据的技术证据，但要求保留 requirement 和 source 引用。
