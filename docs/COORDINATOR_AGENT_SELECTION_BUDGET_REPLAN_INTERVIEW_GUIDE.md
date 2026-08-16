# Coordinator 初始规划、预算与 Evidence Gate：当前实现

> 本文只描述当前生产代码。历史 Dynamic Replan、Agent handoff、`replanCount` 和顶层 `AgentOutput.confidence` 已删除。

## 1. 产品真实入口

当前产品对用户只提供一条业务路径：

```text
上传简历（可附JD）
→ 完整简历评估
→ 最终结构化报告
```

代码中保留的其他 `runType` 配置属于内部兼容/预留，不应在面试中描述成已经开放的产品能力。

## 2. Coordinator 当前负责什么

Coordinator 只在 Run 开始时执行一次初始规划，不在运行中重新进入。

输入包括：

- 简历和 JD 是否存在；
- 项目、时间线、外部 URL 等输入信号；
- artifact 依赖；
- Policy 的 LLM、工具、token 和耗时上限；
- 恢复时已经存在且可以复用的 artifact。

输出包括：

```json
{
  "plan": [
    "TechAgent",
    "ProjectAgent",
    "RiskAgent",
    "EvidenceAgent",
    "ReportAgent"
  ],
  "parallelGroups": [
    ["TechAgent", "ProjectAgent", "RiskAgent"],
    ["EvidenceAgent"],
    ["ReportAgent"]
  ],
  "budgetPlan": {
    "TechAgent": {
      "llmQuota": 2,
      "actionTurnQuota": 1,
      "toolQuota": 6
    }
  },
  "selectedBecause": {},
  "skippedBecause": {},
  "artifactEdges": [],
  "goalArtifacts": []
}
```

Coordinator 不产出技术判断、项目结论、风险或最终报告。

## 3. 当前执行链

```text
Coordinator（初始规划）
        ↓
Tech + Project + Risk（Send并行）
        ↓
Evidence
        ↓
Report
        ↓
finalize
```

每个并行组在 merge 后保存 checkpoint，然后直接进入 dispatch 取下一组。运行期间：

- 不修改 `plan`；
- 不插入 Agent；
- 不接受 Agent handoff；
- 不重新执行已完成 Agent；
- 不调用 Coordinator 做 Replan。

## 4. 为什么删除 Dynamic Replan

当前 Run 的简历和 JD 在开始后不发生变化，也没有中途用户补充。Evidence 发现某个主张没有原文支持时，重新运行 Tech、Project 或 Risk 不能产生新证据。

旧实现还存在三个实际问题：

1. 每组都经过 Replan 节点，但绝大多数只是 `replanned=false`；
2. `low_confidence` 来自 Agent 自报分数，没有做业务校准；
3. 完整评估禁用了 Coordinator Replan LLM，很多 trigger 能检测却不能修改计划。

删除以后，控制流和故障处理职责分开：

| 情况 | 当前处理 |
|---|---|
| 网络、LLM、工具临时失败 | LLM/工具调用层有限重试 |
| JSON 或 Schema 不合法 | 当前 Agent 输出修复 |
| Agent 最终失败 | 保留其他产物并降级收口 |
| 主张缺少简历证据 | Evidence Gate |
| 进程宕机 | LangGraph checkpoint + execution_snapshot |

## 5. Evidence Gate

EvidenceAgent 使用 `verify_report_evidence` 核验上游 findings、risks 和 recommendations。候选主张提取兼容：

```text
text → finding → claim → detail
```

最终 Report 不依赖模型自报 confidence，而是由运行时读取：

```text
evidence[].verified
evidenceSupportRatio
conflicts[].resolution
```

确定性规则：

```text
没有可核验项
→ dataQuality最多INSUFFICIENT
→ 删除overallScore

存在unsupported、非keep冲突或supportRatio < 0.85
→ dataQuality最多PARTIAL
→ 相关主张加入missingEvidence

证据支持率达标且无冲突
→ 正常保留Report结果
```

即使 ReportAgent 忽略 Prompt，运行时后处理仍会执行以上限制。

## 6. 顶层 confidence 删除范围

已删除：

```text
AgentOutput.confidence
Agent输出Schema中的confidence
agent.completed事件中的confidence
avg_confidence / REPLAN_CONFIDENCE_THRESHOLD
```

仍然存在的同名字段不属于 Agent 自报路由信号，例如：

- RAG 检索分数；
- Memory 检索/写入可信度；
- 简历解析完整度辅助值；
- 单条候选人风险的可选描述字段。

这些字段不会触发 Workflow 路由。

## 7. LangGraph 当前负责什么

LangGraph 负责：

- Coordinator 初始规划节点边界；
- `Send` 并行分发；
- reducer 合并并行结果；
- merge 后持久化；
- dispatch 下一组；
- finalize；
- checkpoint 恢复。

RunExecutor 继续负责：

- Agent Prompt；
- LLM 和工具调用；
- Skill 与 Memory；
- 预算；
- Agent 输出校验；
- Evidence Gate；
- 最终报告渲染。

## 8. 面试回答

### 为什么没有 Dynamic Replan？

> 我们的输入是单次提交的简历和 JD，Run 内没有新增信息。离线观察发现通用 Replan 大量空转，而且 Evidence 发现缺证据后重新运行 Specialist 不能创造新事实。因此删除中途 Replan，改用 LLM/工具有限重试、结构化校验和 Evidence Gate，降低延迟和状态复杂度。

### Coordinator 还有什么价值？

> Coordinator 是一次性的 Workflow Planner。它根据 artifact 依赖、输入信号和全局预算生成并行组及每个 Agent 的 LLM/tool quota。它不参与业务结论，也不会在运行中重新进入。

### Evidence 发现问题为什么不回跑？

> 需要区分“调用失败”和“来源不存在”。调用失败由 LLM/工具调用层重试，Agent 最终失败则降级；来源不存在时，重跑只会重复推理。系统会把不支持的主张移入 missingEvidence，限制 dataQuality，并转化成面试核验项。

### 这还是多 Agent 系统吗？

> 是。Tech、Project、Risk 仍然是独立职责、独立 Prompt、独立 Skill/tool 权限和独立结构化产物，Evidence 做跨 Agent 证据校验，Report 聚合。删除的是收益不足的动态循环，不是 Specialist 分工。
