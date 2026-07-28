---
name: calibrate-and-explain-decision
description: 对评估 claim 的证据状态与置信度做最终校准，并基于当前不可变 revision 向用户解释分数、差距、建议和未知项。Evidence、Report 与 InterviewQuestion Agent 使用；需要融合来源、处理冲突、回答“为什么”或生成有证据依据的追问时使用。
version: v1
---

# Calibrate And Explain Decision

本 Skill 将“证据融合”和“结论解释”收敛为同一条可审计链：先得到 claim 状态，再生成用户可理解的结论。需要完整字段时再读取 `references/decision-contract.md`。

## 输入

- `claimLedger`：原子 claim、证据、冲突和 tool health。
- `reportRevision`：当前报告、revision ID 与 `supersedesRevision`。
- `scoreBreakdown`：JD 维度、rubric、分数和计算理由。
- `question`：可选，用户当前追问或需要生成的面试核实点。

## 工作流

1. 按 `claimId` 聚合并去重同源证据。
2. 分开保存候选人自述、用户补充、RAG 定位和真实外部工具结果。
3. 只接纳成功、带 `sourceRef` 的外部结果；账号内容真实不等于身份已绑定。
4. 输出 `supported | partially_supported | unsupported | conflicted | not_checked`；`unsupported` 必须有可靠反证。
5. 解释时绑定具体 claim、维度或建议，引用原始证据而不是引用报告自身。
6. 清楚区分已知事实、推断、未知和工具未检查；不以“模型判断”作为理由。
7. 用户补充只先记为 `user_statement`。若改变简历、JD、偏好、重点或证据输入，交给 `plan-evaluation-revision`，不覆盖旧结果。
8. 面试问题必须指向具体证据缺口，并给出好信号、红旗和后续追问。

## 输出

```json
{
  "claims": [{
    "claimId": "redis-depth",
    "status": "not_checked",
    "confidenceBand": "low",
    "supportingRefs": ["resume:p1:l20"],
    "conflictingRefs": [],
    "reason": "简历列出 Redis，但没有可定位的设计或排障经历",
    "missingEvidence": ["design_or_incident_example"]
  }],
  "directAnswer": "该项是证据不足，不是能力不足。",
  "revisionRequired": false,
  "affectedArtifacts": [],
  "interviewProbes": [],
  "unknowns": [],
  "toolHealth": {}
}
```

## 边界

- `not_checked` 绝不能降级成 `unsupported`、造假或负面风险。
- 工具失败只进入 `toolHealth` 和 `unknowns`。
- 不因来源数量、外部结果正面或用户质疑而机械加减分。
- 无依据的旧结论要明确承认并建议新 revision；不能为了维护旧报告拒绝修正。
- 本 Skill 不决定重新运行哪些节点；revision 失效范围由 `plan-evaluation-revision` 负责。
