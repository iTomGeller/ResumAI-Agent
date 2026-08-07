# 一份简历如何经过五个 Agent 变成最终报告

> 2026-08-07 当前契约：JD、当前简历、知识库三类 RAG 都在 Agent 生成前由 Runtime 固定召回，并作为 `[RAG上下文]` 直接放入 user prompt；它们不是 tool call。ReportAgent 只有一次完整结构化输出，不再拆 score/risk/question。Memory 仅保留 Semantic、Episodic、Procedural，Working 不再读写。

> 这份文档只做一件事：拿最新100份压测中的一份真实输入，从上传开始，按执行顺序讲清每个 Agent 此刻看见什么、判断什么、写出什么，以及下一个 Agent 怎么使用。  
> 原总报告 `MULTI_AGENT_ARCHITECTURE_DEEP_DIVE.md` 不修改。

## 先看完整过程

```text
简历 + 匹配到的 JD
        ↓
确定性预处理：解析简历、检索 JD、整理共享事实与各 Agent 的 `[RAG上下文]`
        ↓
┌──────────────┬──────────────┬──────────────┐
│ TechAgent    │ ProjectAgent │ RiskAgent    │
│ 看技术证据   │ 看项目深度   │ 看履历风险   │
└──────────────┴──────────────┴──────────────┘
        ↓ 三者结果统一写进共享黑板
EvidenceAgent：检查这些结论到底有没有证据
        ↓
ReportAgent（唯一一次调用）：同时产出评分、风险、面试题、最终建议
        ↓
finalReport
```

先记住两件事：

1. Tech、Project、Risk 是并行的，它们不会互相聊天，也看不到彼此同时产生的结果。
2. Agent 之间靠 `SharedState.data.artifacts` 共享结构化结果，不靠自然语言转述。

---

## 1. 这次用哪一份真实压测 Case

最新100份中的代表 Run：

| 字段 | 实际值 |
|---|---|
| 简历编号 | `senior_backend_004` |
| Run ID | `run-e17f6fba-c37d-4a65-b7be-b5e28580942e` |
| 状态 | `SUCCEEDED` |
| Runtime | 54.212s |
| LLM 调用 | 7次 |
| 工具调用 | 8次 |
| 最终分数 | 69 |
| 最终建议 | `INTERVIEW_RECOMMEND` |

### 1.1 输入简历讲了什么

先不看完整原文，只提炼这份简历里真正影响评估的事实：

```text
候选人：Feng Ting
方向：Senior Backend Engineer
经历：
  2014.07 - 2017.06  Lalamove Backend Engineer
  2017.07 - Present  NIO Senior Backend Engineer

关键技术事实：
  - JVM：GC log + heap dump + arthas 定位无界缓存，内存降低46%
  - Kafka：幂等键、重试队列、死信队列，峰值20000 QPS
  - MySQL：gh-ost在线DDL，P99从1200ms降到380ms
  - Redis：热Key、缓存击穿、本地缓存、分布式锁，命中率超过90%
  - 可观测性：Prometheus + Grafana

项目：
  - 高并发秒杀与库存系统
  - 分布式支付结算平台
  - 企业鉴权与网关平台

明显缺口：
  - 没写Java具体版本
  - 没写Spring Boot具体版本
  - 没有RAG/LLM实际项目
  - 20000 QPS等指标没有压测环境和测量口径
```

### 1.2 实际匹配到的 JD

```text
招聘 Java 21 / Spring Boot 3 / AI Agent 平台方向高级后端工程师，
要求熟悉 RAG、Trace 可观测、Docker 部署、线上问题排查和端到端交付。
必要技能：Java, Spring Boot, MySQL, Redis, Docker, RAG, LLM。
经验要求：5年以上。
```

检索结果：

```json
{
  "jdId": "job-java-agent",
  "title": "高级 Java / AI Agent 平台工程师",
  "skillMatchScore": 0.7143,
  "experienceMatchScore": 1.0,
  "projectMatchScore": 0.59,
  "matchScore": 0.6894,
  "gaps": ["未明确体现：rag", "未明确体现：llm"],
  "version": "43"
}
```

这里的“真实”表示它确实是本轮100份压测使用的数据。简历是压测样本，不代表已验证身份的真人；`job-java-agent` 是测试 JD 种子，不是招聘网站抓取的真实职位。

### 1.3 为什么下面会出现两个 Run ID

最新100份归档保存了完整简历、最终报告和逐 Agent 时延，但没有保存每个 Agent 的原始中间 JSON。

项目里还保留了同一份 `senior_backend_004` 输入的完整 Trace：

```text
run-3184e49a-5a7e-470f-bf32-325d17503027
```

因此本文遵循下面的口径：

- 标注“100份代表 Run”的，是最新成功压测的真实最终结果。
- 标注“同样本完整 Trace”的，是同一份简历和 JD 的真实中间对象。
- 两者不是同一次执行，不能把两个 Run 的分数和时延混在一起。

---

## 2. 第一步不是 Agent，而是确定性预处理

简历进入 Workflow 后，Coordinator 先做两件事：

```text
parse_resume
jd_match_search
```

这两步不需要 LLM 判断，目的是先把原始文本变成后续 Agent 都能读的共享事实。

### 2.1 `parse_resume` 实际解析出了什么

同样本完整 Trace 中的实际结果：

```jsonc
{
  "success": true,
  "chars": 4490,
  "skills": [
    "agent", "docker", "go", "grafana", "java", "jvm", "kafka",
    "mysql", "prometheus", "redis", "rocketmq", "spring",
    "spring boot", "spring cloud"
  ],
  "projectNames": [],
  "timelinePeriods": [
    {"raw": "2010.09 - 2014.06", "line": 5, "openEnded": false},
    {"raw": "2017.07 - Present", "line": 13, "openEnded": true},
    {"raw": "2014.07 - 2017.06", "line": 23, "openEnded": false}
  ],
  "confidence": 0.9
}
```

这里已经能看到解析器的两个真实问题：

- `go` 是按子串扫描出来的，未必代表候选人真的有 Go 项目。
- 简历明明有英文 `Projects` 段，但 `projectNames=[]`，说明项目标题解析对英文格式不够稳。

所以后续 Agent 不能只看 `skills/projects`，还必须读 `rawExcerpt` 和语义检索结果。

### 2.2 写入共享黑板的初始状态

预处理结束后，可以把共享黑板理解成：

```jsonc
{
  "artifacts": {
    "resumeFacts": {
      "skills": [
        "agent", "docker", "go", "grafana", "java", "jvm", "kafka",
        "mysql", "prometheus", "redis", "rocketmq", "spring",
        "spring boot", "spring cloud"
      ],
      "projects": [],
      "experiences": [
        {"raw": "2017.07 - Present    NIO    Senior Backend Engineer"},
        {"raw": "...arthas... unbounded cache... cut memory by 46%..."},
        {"raw": "...Kafka... idempotency keys... 20000 QPS..."},
        {"raw": "2014.07 - 2017.06    Lalamove    Backend Engineer"}
      ],
      "education": [
        {"raw": "2010.09 - 2014.06 Zhejiang University Computer Science..."}
      ],
      "timelinePeriods": [ /* 3段时间 */ ],
      "rawExcerpt": "原始简历前3000字符",
      "source": "parse_resume_fast_path",
      "confidence": 0.9
    },
    "effectiveJd": "招聘 Java 21 / Spring Boot 3 / AI Agent 平台方向高级后端工程师...",
    "jdMatches": [ /* 上面的job-java-agent检索结果 */ ],
    "inputPresence": {
      "resumePresent": true,
      "jdPresent": true,
      "hasJdMatches": true
    },
    "technicalFindings": [],
    "projectFindings": [],
    "risks": [],
    "evidence": [],
    "conflicts": [],
    "recommendations": []
  }
}
```

到这里还没有“技术评分”“项目风险”或“面试题”。它只有输入事实。

---

## 3. 第二步：Tech、Project、Risk 并行分析

这三个 Agent 同时开始，所以它们拿到的是同一份初始状态快照：

```text
TechAgent    不会先看到 ProjectAgent 的判断
ProjectAgent 不会先看到 TechAgent 的判断
RiskAgent    不会先看到前两者的判断
```

它们各自完成后，Runtime 再统一合并。

### 3.1 TechAgent：判断技术能力有没有证据

#### 它为什么存在

它解决的问题不是“候选人写没写 Java”，而是：

```text
只在技能栏出现？
还是在项目里真正使用过？
有没有设计、调优、故障排查和规模证据？
与 JD 的具体版本和方向是否匹配？
```

#### 它实际看到什么

```jsonc
{
  "resumeFacts": {
    "skills": ["java", "jvm", "kafka", "mysql", "redis", "spring boot", "docker", "agent"],
    "projects": [],
    "experiences": [
      {"raw": "...arthas... cut memory by 46%..."},
      {"raw": "...Kafka... 20000 QPS..."},
      {"raw": "...gh-ost... P99 1200ms to 380ms..."}
    ],
    "rawExcerpt": "包含完整项目段的原文"
  },
  "effectiveJd": "Java 21 / Spring Boot 3 / RAG / LLM / Trace / Docker...",
  "inputPresence": {
    "resumePresent": true,
    "jdPresent": true
  }
}
```

TechAgent 开始前还会执行：

```text
calculate_jd_coverage
resume_semantic_search
knowledge_search
```

这份 Case 的实际 `jdCoverage`：

```json
{
  "requirementCount": 1,
  "coveredCount": 1,
  "coverage": 1.0,
  "perRequirement": [
    {
      "matchedTerms": ["java", "spring", "boot", "ai", "agent", "trace"],
      "matchRatio": 0.875,
      "covered": true
    }
  ]
}
```

这里必须注意：`coverage=1.0` 只表示那一整条长 requirement 被判定为 covered，不表示 RAG、LLM、Java 21 全部满足。最终 JD 匹配只有52分。

#### 它实际写出了什么

同样本完整 Trace 中的真实 `technicalFindings`：

```json
[
  {
    "id": "t1",
    "claim": "JVM调优深度强——通过GC日志、heap dump、arthas定位无界缓存，内存降低46%",
    "depth": "troubleshot",
    "status": "supported",
    "sourceRefs": ["简历第14-15行...cut memory by 46%"],
    "byAgent": "TechAgent"
  },
  {
    "id": "t2",
    "claim": "Kafka异步事务管道设计——幂等键、重试队列、死信处理，支撑20000 QPS峰值",
    "depth": "designed",
    "status": "supported",
    "sourceRefs": ["简历第17-18行...sustaining 20000 QPS"],
    "byAgent": "TechAgent"
  },
  {
    "id": "t5",
    "claim": "RAG/LLM/AI Agent相关技术——JD核心方向，简历无任何项目或实践证据",
    "depth": "mentioned",
    "status": "unsupported",
    "sourceRefs": ["简历全文没有RAG、LLM项目"],
    "byAgent": "TechAgent"
  },
  {
    "id": "t7",
    "claim": "Java 21 / Spring Boot 3——JD明确要求，简历无版本号证据",
    "depth": "mentioned",
    "status": "unsupported",
    "sourceRefs": ["技能栏只有Java、Spring Boot，没有版本号"],
    "byAgent": "TechAgent"
  }
]
```

一句话理解：TechAgent 把“简历上的技术名词”变成了“有证据的能力、部分证据的能力、没有证据的缺口”。

### 3.2 ProjectAgent：判断项目是否真的有深度

#### 它为什么存在

项目写了20000 QPS并不等于这个数字可信。ProjectAgent 专门问：

```text
项目复杂吗？
候选人本人做了什么？
技术选型合理吗？
量化指标有没有基线和测量条件？
项目与工作经历的业务背景对得上吗？
```

#### 它实际看到什么

ProjectAgent 看到 `resumeFacts + effectiveJd`，但看不到并行中的 TechAgent 输出：

```jsonc
{
  "resumeFacts": {
    "projects": [],
    "rawExcerpt": "包含High-Concurrency Flash-Sale、Payment & Settlement等项目原文",
    "experiences": ["NIO...", "Lalamove..."]
  },
  "effectiveJd": "Java 21 / Spring Boot 3 / AI Agent / RAG / LLM..."
}
```

虽然 `projects=[]`，它仍可通过 `rawExcerpt` 和 `resume_semantic_search` 找项目。它还会加载：

```text
ground-project-claims
retrieve-public-candidate-evidence
```

简历有 GitHub URL 时，它可以选择调用 `fetch.fetch`；成功结果由 Runtime 写入 `mcpEvidence`，不是由 ProjectAgent 自己编造。

#### 它实际写出了什么

同样本完整 Trace 中的真实 `projectFindings`：

```json
[
  {
    "id": "p1",
    "finding": "项目归属不明确——可能是团队项目或个人项目",
    "detail": "秒杀库存系统、支付结算平台均未标注是工作项目还是个人项目，Built/Designed/Implemented没有区分个人与团队贡献。",
    "severity": "medium_risk",
    "byAgent": "ProjectAgent"
  },
  {
    "id": "p2",
    "finding": "QPS和SLA指标缺乏基线、时间窗和测量方式",
    "detail": "20000 QPS、6460K requests/day、99.9% SLA、error rate below 0.1%没有说明测试工具、环境和统计口径。",
    "severity": "medium_risk",
    "byAgent": "ProjectAgent"
  },
  {
    "id": "p3",
    "finding": "秒杀系统与NIO/Lalamove业务场景关联存疑",
    "detail": "NIO是车企，Lalamove是货运物流，简历没有解释秒杀系统的业务上下文。",
    "severity": "medium_risk",
    "byAgent": "ProjectAgent"
  },
  {
    "id": "p4",
    "finding": "支付结算平台设计合理但缺乏规模数据",
    "detail": "分库分表、幂等、每日对账和审计追踪合理，但没有交易量、对账延迟和修正成功率。",
    "severity": "low_risk",
    "byAgent": "ProjectAgent"
  }
]
```

一句话理解：ProjectAgent 不否定项目描述，而是把“项目看起来很强”拆成“架构合理的部分”和“必须面试核验的部分”。

### 3.3 RiskAgent：找履历自身的异常，而不是再做一遍技术评分

#### 它为什么存在

TechAgent 和 ProjectAgent 关心的是“能力够不够、项目深不深”。RiskAgent 关心的是另一类问题：

```text
时间线有没有冲突、空窗？
技能是否只出现于关键词列表、没有经历支撑？
量化成果是否缺少测量口径？
教育、任职、个人贡献等事实是否仍待核实？
```

它的价值不是把所有不确定内容都判成造假，而是把后续面试必须核实的点列出来。

#### 它实际看到什么

当前代码给 RiskAgent 的共享状态视图只有：

```text
resumeFacts
timelineCheck
inputPresence
```

注意：它看不到 `jdRequirements` 和 `effectiveJd`，也看不到同一轮并行执行的 TechAgent、ProjectAgent 结果。因此：

- 时间线、履历漂移、简历内部自相矛盾，是 RiskAgent 的直接职责；
- RAG/LLM 是否符合 JD，主要应由 TechAgent、EvidenceAgent 和 ReportAgent 判断；
- 三个并行 Agent 不会互相抄答案，也不会在并行阶段互相等待。

这份简历进入 RiskAgent 前，确定性工具已经产出了真实 `timelineCheck`：

```json
{
  "success": true,
  "periodCount": 3,
  "periods": [
    {"type": "education", "start": "2010.09", "end": "2014.06"},
    {"type": "experience", "start": "2017.07", "end": "Present", "company": "NIO"},
    {"type": "experience", "start": "2014.07", "end": "2017.06", "company": "Lalamove"}
  ],
  "overlaps": [],
  "gaps": [],
  "issues": [],
  "hasHighRisk": false
}
```

这意味着 RiskAgent 不应该编造“工作时间冲突”或“存在职业空窗”。这个 Case 真正值得核实的是量化口径、项目归属和履历真实性，而不是时间线。

#### 这一段能展示到什么程度

这里必须把数据边界讲清楚：

- 最新100份批次中，该 Run 的 RiskAgent 成功完成，耗时 `16.386s`；
- 但批次归档没有保存该次 RiskAgent 的原始 `AgentDecision`；
- 现存的同输入完整 Trace 中，RiskAgent 那次执行失败，所以不能拿它冒充最新 Run 的成功原文。

因此下面只能展示最新 Run 最终报告中已经落地的候选人风险。它们是经过 Evidence/Report 收口后的结果，不等同于 RiskAgent 原始输出：

```json
[
  {
    "severity": "HIGH",
    "risk": "JD核心方向RAG/LLM/AI Agent在简历全文零覆盖",
    "verification": "面试中要求候选人描述一个具体RAG应用的架构；若仅停留在概念层面，则判定不匹配"
  },
  {
    "severity": "MEDIUM",
    "risk": "内存下降46%、20000 QPS、P99 1200ms降至380ms等指标缺少测量方式、时间窗与环境规模",
    "verification": "追问原始基线、机器配置、压测工具、并发模型和观测时间窗"
  }
]
```

一句话理解：RiskAgent 把“看上去可疑”改写成“可以在面试中被验证或推翻的问题”，但现有归档不足以逐字复原这次成功调用的原始答案。

---

## 4. 三个并行 Agent 完成后：Reducer 把结果合进同一块黑板

Tech、Project、Risk 并行时彼此不可见。它们完成后，LangGraph 的 Reducer 才把各自产物合并进共享状态：

```text
TechAgent     ── technicalFindings ─┐
ProjectAgent  ── projectFindings  ──┼─> Reducer merge ─> SharedState
RiskAgent     ── risks            ──┘
```

这个 Case 合并后的状态可以简化理解为：

```jsonc
{
  "resumeFacts": {"...": "解析后的简历事实"},
  "jdRequirements": {"...": "JD要求"},
  "technicalFindings": [
    {"claim": "JVM排障使内存下降46%", "byAgent": "TechAgent"},
    {"claim": "RAG/LLM无简历证据", "byAgent": "TechAgent"}
  ],
  "projectFindings": [
    {"finding": "20000 QPS缺少压测环境和测量方式", "byAgent": "ProjectAgent"}
  ],
  "risks": [
    {"risk": "需要核实量化口径", "byAgent": "RiskAgent"}
  ]
}
```

这里没有第四个 Agent 手工复制粘贴结果。Reducer 根据状态字段的合并规则，把并行节点返回的增量写进同一份 `RuntimeGraphState`。

需要注意一个真实的工程现状：Finding 目前是弱类型字典，不同 Agent 的主文本键可能是 `claim`、`finding`、`risk` 或 `detail`。运行没有因此失败，但消费方需要兼容多种键名。这是当前代码契约的真实样子，不是最理想的统一模型。

---

## 5. EvidenceAgent：不重新评简历，而是审计前三个 Agent 的说法

### 它为什么在并行组之后

EvidenceAgent 必须先看到 Tech、Project、Risk 的结论，才能逐条回答：

```text
这句话在简历或JD中有直接来源吗？
来源支持的是“简历确实写了”，还是已经足以证明“这件事可信”？
不同Agent之间有没有冲突？
哪些结论只能标成not_checked？
```

所以它不能和前三个 Agent 同时启动。

### 它实际消费什么

合并后，EvidenceAgent 得到：

```text
resumeFacts
jdRequirements
technicalFindings
projectFindings
risks
mcpEvidence
inputPresence
```

以当前 Case 为例，它看到的待审计说法包括：

```json
[
  {
    "byAgent": "TechAgent",
    "claim": "候选人使用GC logs、heap dump和Arthas定位无界缓存，内存下降46%"
  },
  {
    "byAgent": "ProjectAgent",
    "claim": "20000 QPS和SLA指标缺少基线、时间窗和测量方式"
  }
]
```

### 它先让模型归纳，再让工具做确定性核验

EvidenceAgent 不是仅凭模型说一句“我觉得可信”。模型先生成需要核验的 evidence/conflict 草案，随后 `verify_report_evidence` 用原始简历、JD、MCP结果和来源行做确定性校验。

同一输入完整 Trace 中，真实 evidence 是：

```json
[
  {
    "text": "JVM排障使用GC logs、heap dump和Arthas定位无界缓存，内存下降46%",
    "source": "resume",
    "sourceLine": 14,
    "verified": true,
    "byAgent": "TechAgent"
  },
  {
    "text": "Kafka异步交易管道使用幂等键、重试队列和死信处理，峰值20000 QPS",
    "source": "resume",
    "sourceLine": 17,
    "verified": true,
    "byAgent": "TechAgent"
  },
  {
    "id": "e2",
    "text": "QPS和SLA指标没有给出基线、时间窗和测量方式",
    "source": "resume",
    "sourceLine": "18-21",
    "verified": false,
    "byAgent": "ProjectAgent"
  }
]
```

为什么第三条是 `verified=false`？因为“没有写测量方式”是对缺失信息的判断，无法像原文引句那样用某一行直接正向证明。它可以是合理的审计结论，但不能冒充一条被原文直接证实的事实。

真实 conflicts 中还有：

```json
[
  {
    "type": "unsupported_claim",
    "claim": "项目归属不明确——可能是团队项目或个人项目",
    "reason": "no_source_line",
    "byAgent": "EvidenceAgent"
  },
  {
    "type": "unsupported_claim",
    "claim": "支付结算平台设计合理但缺乏规模数据",
    "reason": "no_source_line",
    "byAgent": "EvidenceAgent"
  }
]
```

这两个 `unsupported_claim` 不是说结论必然错误，而是说当前结论没有绑定到可复核的原文位置，ReportAgent 不能把它包装成已证实事实。

最关键的区别是：

```text
“简历写了20000 QPS”                         → 可由原文直接验证
“候选人的系统真实达到生产峰值20000 QPS”       → 仅凭简历无法验证
“简历没有说明20000 QPS的机器配置和压测工具”   → 合理缺口判断，但不是外部背调证明
```

最新100份批次中，该 Run 最终的 `evidenceSupportRatio=0.692`。上面的逐条中间产物来自同输入完整 Trace，其支持率是 `0.786`；二者不能混写为同一次运行结果。

一句话理解：EvidenceAgent 负责给上游结论贴“有原文支持、无原文支持、互相冲突、尚未核验”的标签，防止 ReportAgent 把推断写成事实。

---

## 6. ReportAgent：只做最后收口，不再上网查资料

### 它为什么最后执行

ReportAgent 消费的是已经合并、已经审计的黑板：

```text
resumeFacts + jdRequirements + effectiveJd
+ technicalFindings + projectFindings + risks
+ evidence + conflicts + recommendations
+ jdCoverage + timelineCheck + mcpEvidence + inputPresence
```

它不暴露公网 MCP。也就是说，最终报告阶段不能临时搜索一个网页，再绕过 EvidenceAgent 把新事实塞进结论。

### 它具体产出什么

这次最新 Run 中，ReportAgent 使用3次 terminal LLM 调用，分别完成评分、风险和面试问题的结构化收口。最终不是一大段自由文本，而是结构化报告加可展示文本。

#### 6.1 最终结论

```json
{
  "overallScore": 69,
  "recommendation": "INTERVIEW_RECOMMEND",
  "dataQuality": "PARTIAL",
  "summary": "候选人拥有约11年后端开发经验，传统Java后端、支付交易、高并发和生产排障能力较强；最大缺口是JD核心要求的RAG/LLM/AI Agent方向完全没有简历证据。建议进入面试，重点核验AI方向能力和量化指标真实性。"
}
```

#### 6.2 四个维度的真实得分

| 维度 | 分数 | 数据状态 | 为什么 |
|---|---:|---|---|
| 技术能力 | 78 | `ASSESSED` | JVM、MySQL、Redis、Kafka等有生产描述和量化结果；版本号、Docker落地和RAG经验不足 |
| 项目深度 | 75 | `ASSESSED` | 秒杀、支付、网关项目结构完整；压测条件、指标基线和个人贡献边界不清楚 |
| JD匹配 | 52 | `ASSESSED` | 传统后端要求大部分覆盖，但RAG/LLM/AI Agent零覆盖 |
| 履历可信度 | 65 | `PARTIAL` | 时间线合理，量化指标有原文但缺测量口径，任职和学历尚未背调 |

`PARTIAL` 很重要：它表示系统完成了简历内审计，但没有把简历陈述误当成任职背调或外部事实核验。

#### 6.3 一个最终面试问题到底长什么样

下面是该 Run 的真实问题 `q3`，它不是一句泛泛的“请介绍项目”：

```json
{
  "id": "q3",
  "priority": "HIGH",
  "question": "简历提到'cut memory by 46%'（NIO内存优化）和'20000 QPS'（Kafka管道），但缺少基线说明。请详细说明：内存优化前的RSS基线是多少？20000 QPS的测试环境规模、压测工具和并发模型是什么？",
  "objective": "核验量化指标的真实性与测量方法，防止指标包装",
  "triggeredBy": "ProjectAgent：'cut memory by 46%'和'20000 QPS'缺少基线说明",
  "goodSignals": [
    "能给出具体基线数字，例如RSS从2GB降到1.08GB",
    "能说明JMeter/wrk/k6等压测工具、机器规格和并发线程数"
  ],
  "redFlags": [
    "无法回忆基线数字，只说大概",
    "压测环境与生产环境差异过大且无法解释"
  ],
  "followUps": [
    "内存优化后如何验证没有引入新的性能回退？"
  ],
  "evidenceRefs": [
    {"sourceType": "RESUME", "quote": "found an unbounded cache and cut memory by 46%"},
    {"sourceType": "RESUME", "quote": "sustaining 20000 QPS at peak"}
  ]
}
```

这里能清楚看到上游产物如何被消费：ProjectAgent 提出“量化口径不完整”，EvidenceAgent 保留原文证据和证据边界，ReportAgent 把它变成有目标、有正反信号、有追问的面试题。

一句话理解：ReportAgent 不负责重新研究候选人，它负责把共享黑板中已经校准的内容变成评分、风险、证据和可执行的面试问题。

---

## 7. 从一句简历原文，看它怎样穿过五个 Agent

### 例子一：`cut memory by 46%`

```text
简历原文
  候选人用GC logs、heap dump、Arthas找到无界缓存，内存下降46%
      ↓
TechAgent
  识别为“有具体排障工具和结果的JVM能力证据”
      ↓
ProjectAgent
  发现46%缺少优化前RSS、观测周期和环境信息
      ↓
EvidenceAgent
  确认简历确实写了46%，但不能确认这个指标客观真实
      ↓
ReportAgent
  技术能力加分，同时生成追问：原始RSS是多少，如何验证无性能回退？
```

### 例子二：`20000 QPS`

```text
简历原文
  Kafka异步交易管道峰值20000 QPS；秒杀项目压测20000 QPS
      ↓
TechAgent
  识别幂等、重试、死信队列和高并发经验
      ↓
ProjectAgent
  区分两个20000 QPS说法，并指出没有机器规格、工具和并发模型
      ↓
EvidenceAgent
  “原文存在”=true；“生产能力已被证实”=false
      ↓
ReportAgent
  项目深度加分，但把指标可信度列为MEDIUM风险并生成HIGH优先级问题
```

### 例子三：JD要求RAG/LLM，但简历没有

```text
JD
  必要技能包含RAG、LLM
      ↓
TechAgent
  在技能、经历和项目中都找不到直接证据
      ↓
ProjectAgent
  三个项目均为传统Java后端项目，没有AI Agent项目
      ↓
EvidenceAgent
  把“零覆盖”保留为证据缺口，不虚构候选人不会学习或一定不能胜任
      ↓
ReportAgent
  JD匹配52分，列为HIGH风险，并要求候选人现场说明相关经验或设计RAG系统
```

这三条链路说明当前架构不是五个 Agent 各写一份报告，而是同一事实经过“识别能力 → 质疑口径 → 校验证据 → 形成决策”的逐步加工。

---

## 8. 最后再看一次：每个 Agent 到底吃什么、吐什么

| 阶段 | 真正消费的内容 | 真正产出的内容 | 下一个使用者 |
|---|---|---|---|
| 确定性预处理 | 简历全文、命中的JD | `resumeFacts`、`jdRequirements`、`timelineCheck`、RAG片段 | Tech / Project / Risk |
| TechAgent | 简历事实、JD、技术相关RAG和Memory | `technicalFindings`：技能证据、版本缺口、JD技术覆盖 | EvidenceAgent |
| ProjectAgent | 简历事实、JD、项目相关RAG，必要时MCP URL结果 | `projectFindings`、`mcpEvidence`：复杂度、贡献边界、指标口径 | EvidenceAgent |
| RiskAgent | 简历事实、时间线、输入完整性 | `risks`：时间线、履历、关键词和待核实项 | EvidenceAgent |
| Reducer | 三个并行Agent返回的状态增量 | 合并后的共享黑板 | EvidenceAgent |
| EvidenceAgent | 上游全部Finding、原简历、JD、MCP证据 | `evidence`、`conflicts`、`recommendations` | ReportAgent |
| ReportAgent | 已合并且已校准的全部状态 | `finalReport`：评分、风险、证据、面试题、建议 | MySQL业务结果与前端 |

如果只记一句话：

```text
Tech/Project/Risk负责提出判断，Evidence负责限制这些判断能说到什么程度，Report负责把判断变成最终招聘决策材料。
```

---

## 9. 附录：主线之外的实现细节

### 9.1 Agent返回值为什么看起来有好几层

单个 Agent 的返回值可按三层理解：

```text
AgentDecision
  └─ output: AgentOutput
       └─ artifacts: 对共享状态的字段增量
```

- `AgentDecision`：这次执行是否完成、是否需要继续、调用了什么等运行控制信息；
- `AgentOutput`：给人看的摘要、置信度和结构化结果；
- `artifacts`：真正由 Reducer 写回共享状态的字段，例如 `technicalFindings`。

业务上最重要的是 `artifacts`，因为下一个 Agent 消费的是共享状态，而不是上一位 Agent 的聊天文本。

### 9.2 当前弱类型契约

当前 Finding 尚未统一成一个严格类：

```jsonc
{"claim": "...", "byAgent": "TechAgent"}
{"finding": "...", "byAgent": "ProjectAgent"}
{"risk": "...", "byAgent": "RiskAgent"}
```

这解释了为什么 Evidence/Report 的读取逻辑要兼容多个主文本字段。本文按真实代码说明，不把它美化成尚不存在的统一 Schema。

### 9.3 本 Case 的完整脱敏压测简历

下面是压测文件 `senior_backend_004.pdf` 的文本内容。姓名保留用于和批次记录对应，电话、邮箱、GitHub地址已脱敏。它是项目生成的压力测试数据，不是真实求职者授权提交的简历。

<details>
<summary>展开查看完整脱敏简历</summary>

```text
Feng Ting
Gender: Female    Objective: Senior Backend Engineer    Location: Wuhan
Phone: <REDACTED_PHONE>    Email: <REDACTED_EMAIL>    GitHub: <REDACTED_GITHUB>

Education
2010.09 - 2014.06    Zhejiang University    Computer Science and Technology (B.S.)
Courses: Distributed Systems, Computer Organization, Design Patterns,
Software Engineering, Database Systems, Machine Learning
GPA 3.4/4.0, top 30% in major; merit scholarship

Summary
Seven years of backend experience focused on payment/transaction systems,
distributed architecture, high concurrency and production incident response.

Work Experience
2017.07 - Present    NIO    Senior Backend Engineer
- Investigated rising instance memory RSS using GC logs, heap dump and Arthas,
  found an unbounded cache and cut memory by 46%, reused across multiple business lines.
- Fixed a log-collection failure caused by agent backpressure, restoring stability
  after tuning batch and buffer settings, improving end-to-end delivery quality.
- Designed a Kafka-based async transaction pipeline with idempotency keys,
  retry queues and dead-letter handling, sustaining 20000 QPS at peak.
- Served 6460K requests per day while keeping core-path SLA above 99.9%.
- Led a production incident review with root-cause analysis and drove an
  alert-tiering and on-call rotation mechanism, supporting rapid business growth.

2014.07 - 2017.06    Lalamove    Backend Engineer
- Optimized a file-sort slow SQL on a hot API, added a composite index via gh-ost
  online DDL, reducing P99 from 1200ms to 380ms, reused across multiple business lines.
- Built service observability covering API latency, error rate, JVM GC, thread pools
  and consumer backlog with Prometheus and Grafana.
- Rebuilt the config and template management service to support hot template reload
  without restart, cutting release time by 24%, and key metrics improved steadily.
- Tuned Redis caching for hot keys and cache breakdown with local cache and
  distributed locks, raising hit rate above 90%, supporting rapid business growth.

Projects
High-Concurrency Flash-Sale & Inventory System (Java + Redis + RocketMQ)
- Stayed stable at 20000 QPS in load tests with error rate below 0.1%.
- Built circuit breaking and fallback for downstream failures.
- Designed distributed locks and token-bucket rate limiting to prevent oversell,
  improving end-to-end delivery quality.
- Pre-deducted inventory in Redis with async DB writes to absorb traffic spikes,
  and key metrics improved steadily.

Distributed Payment & Settlement Platform (Spring Boot + MySQL + Kafka + Redis)
- Used sharding and read-write splitting to keep single-table rows within tens of millions.
- Applied idempotency and eventual consistency to avoid duplicate charges on retry.
- Implemented daily reconciliation across payment gateway, ledger and settlement files
  with auto-correction, improving end-to-end delivery quality.
- Recorded an audit trace id for every payment state transition for compliance.

Enterprise Auth & Gateway Platform (Spring Cloud Gateway + OAuth2)
- Unified auth, rate limiting, gray routing and tracing as reusable platform capabilities.
- Shipped a unified SDK to lower integration cost across teams, significantly lowering maintenance cost.
- Added circuit breaking to isolate faulty downstreams.
- Integrated OAuth2 and RBAC with multi-tenant isolation.

Highlights
- Work around JVM tuning and troubleshooting delivered about 46% efficiency gains
  and was reused across the team.
- Established metrics and reviews around distributed transactions and consistency
  to keep improving delivery quality.
- Led a major technical effort involving requirement analysis and documentation,
  staying stable at 20000 QPS peak.
- Built a reusable methodology and docs around design patterns and refactoring,
  adopted by several teams.
- Drove a focused effort on Docker and containerization, reducing related issues by about 24%.

Skills
Core skills: Distributed transactions and consistency, Kafka/RocketMQ messaging,
Java, MySQL, JVM tuning and troubleshooting
Proficient in: MySQL indexing and slow-query tuning, Microservices, Observability,
Redis caching and distributed locks, High Concurrency
Familiar with: Kafka, Redis, JVM, Spring Boot, Docker and containerization, SQL Optimization

Self Evaluation
- Business-minded, good at balancing technical design against delivery timelines.
- Solid engineering fundamentals and strong troubleshooting skills,
  able to own a module end to end.
```

</details>

### 9.4 数据与代码来源

本文使用的事实来自：

- 最新100份批次：`reports/project_cache100_20260803/`；
- 最新 Case：`run-e17f6fba-c37d-4a65-b7be-b5e28580942e`；
- 同输入完整 Trace：`run-3184e49a-5a7e-470f-bf32-325d17503027`；
- 压测简历：`testdata/stress_resumes/senior_backend_004.pdf`；
- Agent状态视图与Reducer：`workflow/app/runtime/state.py`；
- 执行、工具和上下文注入：`workflow/app/runtime/executor.py`、`context.py`、`tools.py`；
- LangGraph编排：`workflow/app/runtime/langgraph_executor.py`。

最后再次强调数据边界：这是一个真实执行过的压力测试 Case；JD和简历内容确实进入了系统并生成了上述结果，但简历由测试生成器生成，不代表真实自然人的履历。
