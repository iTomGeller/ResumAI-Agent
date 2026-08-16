# Agent 输入输出契约：当前四 Agent 链路

## 1. 总链路

```text
简历 + JD
  → 确定性 preflight（parse、JD归一化、RAG上下文）
  → TechAgent ┐
    ProjectAgent ├─ 并行
    RiskAgent ┘
  → Reducer / merge
  → ReportAgent
  → finalReport
```

Coordinator 是一次性控制面，不是第二个报告 Agent。当前没有 EvidenceAgent、人工审批或 Dynamic Replan。

## 2. 公共 AgentOutput

三个 Specialist 仍通过统一外壳提交结构化结果：

```json
{
  "agentId": "TechAgent",
  "type": "technical_findings",
  "claims": [
    {
      "claim": "候选人有 JVM 故障定位经验",
      "evidence": "使用 GC log、heap dump 和 Arthas 定位无界缓存"
    }
  ],
  "evidence": [
    {
      "source": "resume",
      "quote": "cut memory by 46% after locating an unbounded cache"
    }
  ],
  "summary": "具备具体的 JVM 排障工具链证据"
}
```

`AgentOutput` 是传输外壳；真正写入共享状态的业务 artifact 仍分别是 `technicalFindings`、`projectFindings` 和 `risks`。

## 3. TechAgent

输入视图：

```text
resumeFacts + jdRequirements + effectiveJd + jdCoverage + inputPresence
+ 技术相关简历RAG + 技术评价知识库RAG
```

输出 `technicalFindings`，回答技能是否只有关键词、是否有项目实践、深度信号和 JD 技术缺口。结论必须绑定原文或明确标记为缺口。

## 4. ProjectAgent

输入视图：

```text
resumeFacts + jdRequirements + effectiveJd + inputPresence
+ 项目相关简历RAG
+ 必要时的候选人公开 URL 工具回执
```

输出 `projectFindings`，区分项目复杂度、个人职责、团队成果、技术决策和量化结果。公网页面可读只证明页面内容存在，不能自动证明账号归属或候选人贡献。

## 5. RiskAgent

输入视图：

```text
resumeFacts + timelineCheck + inputPresence
```

输出 `risks`，区分明确时间线冲突、口径差异、证据缺失和待面试核验项。未知或未联网核验的信息不得写成造假。

## 6. Reducer 与 merge

三个并行节点分别返回 `AgentOutput`。LangGraph reducer 先拼接 `agent_results`，merge 再按原 dispatch 顺序写入 canonical artifact store，避免并发完成顺序改变最终状态。

## 7. ReportAgent

输入视图：

```text
resumeFacts + jdRequirements + effectiveJd
+ technicalFindings + projectFindings + risks
+ jdCoverage + timelineCheck + mcpEvidence + inputPresence
+ 报告知识库RAG
```

ReportAgent 不暴露公网 MCP，是唯一终态 Agent。它一次产出完整 `finalReport`，包括：

```json
{
  "summary": "候选人 Java 后端基础较好，但高并发指标口径需要复核",
  "dimensions": [
    {
      "name": "技术能力",
      "score": 76,
      "rationale": "有 JVM 排障和缓存治理原文",
      "evidenceRefs": [
        {"sourceType": "RESUME", "quote": "GC logs, heap dump and Arthas"}
      ]
    }
  ],
  "risks": [],
  "interviewQuestions": [],
  "missingEvidence": ["20000 QPS 缺少机器配置、压测工具和时间窗口"],
  "recommendation": "INTERVIEW_RECOMMEND",
  "dataQuality": "PARTIAL"
}
```

三个 Specialist 的结论不是独立核验结果。ReportAgent 只采纳能由简历、JD、RAG上下文或真实工具结果支撑的内容；支撑不足时必须进入 `missingEvidence`、风险或面试追问，不能写成确定事实。

## 8. 一个具体事实怎样流转

```text
简历原文：峰值 20000 QPS
  → TechAgent：识别为高并发经验信号
  → ProjectAgent：指出缺少机器规格、压测工具和并发模型
  → ReportAgent：保留项目能力信号，但把指标口径写入 missingEvidence，生成追问
```

当前架构的关键不是让多个 Agent 重复审核同一句话，而是并行分工后由唯一 ReportAgent 结合原始材料做一次确定性收口。
