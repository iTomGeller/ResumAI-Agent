# Coordinator 初始规划与固定收口：当前实现

## 1. 生产执行链

当前简历评估只有一次 Coordinator 初始规划，没有 Dynamic Replan，也没有 EvidenceAgent：

```text
确定性 preflight（简历解析、JD归一化、RAG）
  → Coordinator 一次生成 plan / parallelGroups / budgetPlan
  → TechAgent ┐
    ProjectAgent ├─ 按输入信号选择并通过 LangGraph Send 并行
    RiskAgent ┘
  → Reducer + merge
  → ReportAgent
  → finalize
```

ReportAgent 是唯一终态 Agent。它直接读取简历、JD、RAG、真实工具回执和三个 Specialist 的结构化结果，输出 `finalReport`。

## 2. Coordinator 真正决定什么

Coordinator 只在 Run 开始时决定：

- 哪些 Specialist 对当前输入有价值；
- 哪些 Agent 可以进入同一个并行组；
- 每个 Agent 的 LLM、action turn 和 tool 配额；
- ReportAgent 的终态预算。

典型完整评估计划：

```json
{
  "plan": ["TechAgent", "ProjectAgent", "RiskAgent", "ReportAgent"],
  "parallelGroups": [
    ["TechAgent", "ProjectAgent", "RiskAgent"],
    ["ReportAgent"]
  ]
}
```

输入没有项目或时间线时，对应 Specialist 可以被省略，但 ReportAgent 始终保留。

## 3. 为什么不实现 Replan

本项目一个 Run 只评估一次已经提交的简历和 JD，Run 中不会出现新的用户输入。重新回到 Coordinator 既不会产生新事实，也不能修复外部服务故障，只会增加一次规划调用和更多恢复状态。

当前故障边界：

| 情况 | 当前处理 |
|---|---|
| 临时 LLM/工具错误 | 调用层有限重试 |
| Agent JSON 不合法 | 当前 Agent 内修复 |
| 某个 Specialist 最终失败 | 保留其他结果，最终报告降级 |
| 简历缺少证据 | ReportAgent 写入 `missingEvidence` 或面试追问，不包装成事实 |
| 进程宕机 | LangGraph checkpoint + `execution_snapshot` 恢复 |

## 4. 删除 EvidenceAgent 后如何控制证据边界

没有额外的“审核 Agent”。证据约束放在离事实最近的两处：

1. Tech、Project、Risk 输出结论时必须附简历/JD/工具来源；
2. ReportAgent 只采纳能在 `resumeFacts`、`effectiveJd`、`RAG上下文` 或真实 `mcpEvidence` 中找到支撑的内容。

支撑不足时，ReportAgent 必须写成 `missingEvidence`、风险或面试追问，不得写成已经证实的候选人事实。这样减少一个串行 LLM 节点，同时保留最终报告的来源约束。

## 5. 面试表述

> 我们用 LangGraph 做一次规划、并行 Specialist 和确定性汇聚。Coordinator 根据目标 artifact、输入信号和预算生成一次固定计划；Tech、Project、Risk 通过 Send 并行，Reducer 按计划顺序合并，单个 ReportAgent 直接结合原始简历、JD、RAG和上游结构化结果完成收口。项目不做通用 Replan，也没有独立 EvidenceAgent：临时失败交给调用层重试，证据不足由 ReportAgent 降级为 missingEvidence 或面试追问。
