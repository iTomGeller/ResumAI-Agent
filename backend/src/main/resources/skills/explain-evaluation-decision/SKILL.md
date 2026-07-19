---
name: explain-evaluation-decision
description: 基于当前报告 revision 和证据台账解释评分、差距与建议。用户追问原因、质疑结论、补充新事实、要求换重点重看，或需要判断哪些节点应重新评估时使用。
---

# Explain Evaluation Decision

回答用户真正质疑的 claim，不为旧报告辩护。旧报告保持不可变；新事实可能创建新 revision。

## 输入

- `question`：当前用户问题或补充。
- `reportRevision`：当前报告及 revision ID。
- `scoreBreakdown`：维度分、rubric 和计算方式。
- `claimLedger`：claim 状态与 source refs。
- `workflowState`：可用 checkpoint 和 artifact revisions。

## 工作流

1. 将问题绑定到具体 claim、分数或建议；找不到时说明范围。
2. 引用报告使用过的原始证据，不以报告自身作为事实来源。
3. 分开说明“已知事实”“推断”“未知”和“工具未检查”。
4. 用户补充事实时先标记为 `user_statement`，不要立即升级为外部验证。
5. 判断补充是否只需解释，还是改变简历、JD、偏好或证据输入。
6. 输入变化时调用 `plan-evaluation-revision`，给出受影响节点，不静默改写旧报告。
7. 如果旧结论缺少依据，明确承认并建议修订。

## 输出

```json
{
  "directAnswer": "该项被标为证据不足，而不是能力不足，因为报告只有技术名词，没有对应项目引用。",
  "claimExplanations": [{
    "claimId": "redis-depth",
    "status": "not_checked",
    "sourceRefs": ["resume:p1:l20"],
    "inference": "简历列出 Redis，但没有可定位的设计或排障经历"
  }],
  "newEvidence": [{"sourceType": "user_statement", "verification": "pending"}],
  "revisionRequired": true,
  "affectedArtifacts": ["resume"],
  "suggestedNextSkill": "plan-evaluation-revision",
  "unknowns": []
}
```

## 边界

- 不使用“模型就是这样判断的”作为解释。
- 不把新口述直接视为独立验证。
- 不因用户质疑而随意调分，也不因维护旧输出而拒绝修正。
- 不覆盖旧 revision；所有改变都保留 `supersedesRevision` 链。
- 无证据时直接说未知，并给出最小补证问题。
