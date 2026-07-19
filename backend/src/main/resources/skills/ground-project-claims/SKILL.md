---
name: ground-project-claims
description: 核验项目复杂度、个人贡献和结果证据，并在不创造事实的前提下改写项目 bullet。评估项目深度、澄清 ownership 或按 JD 优化项目描述时使用。
---

# Ground Project Claims

支持 `mode=assess | rewrite | both`，所有模式共享同一事实台账。

## 输入

接收项目原文、目标 JD requirement、用户已确认的角色、规模、指标、技术决策和 source refs。

## 流程

1. 拆分为问题、行动、技术决策、个人贡献和结果 claim。
2. 将团队成果与个人动作分开，标出 ownership 边界。
3. 检查指标是否包含基线、单位、时间窗和测量方式。
4. 在评估模式输出复杂度、业务价值、贡献和可验证性。
5. 在改写模式仅重排已确认事实；未知信息生成问题或 `[待确认]` 占位符。

## 输出

```json
{
  "claims": [{"claimId": "p1-c1", "status": "candidate_claim", "sourceRefs": []}],
  "assessment": {"complexity": "medium", "contribution": "partially_known", "reason": ""},
  "rewrittenBullets": [],
  "placeholders": [],
  "clarifyingQuestions": []
}
```

## 证据边界

- 不创造人数、QPS、提升比例、技术栈、上线范围或主导角色。
- 公司名气、公司规模和项目是否来自大厂不作为质量分。
- “参与”不自动等于低贡献，“主导”也必须有具体动作支持。
- 无法验证时保留未知，不写成造假风险。
