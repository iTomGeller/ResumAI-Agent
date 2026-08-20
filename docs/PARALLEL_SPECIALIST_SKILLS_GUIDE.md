# 并行 Specialist 组 Skills 详解

> 适用代码版本：当前 `main`。本文只解释第一并行组中的
> `TechAgent`、`ProjectAgent`、`RiskAgent` 及其六个生产 Skill。
> Skill 是按需加载的业务指令包，不是 Agent、RAG，也不是工具调用结果。

## 1. 第一并行组到底做什么

一次完整简历评估完成确定性 preflight 后，第一组通常可以并行执行：

```text
                    ┌─ TechAgent    → technicalFindings
resumeFacts + JD ───┼─ ProjectAgent → projectFindings
                    └─ RiskAgent    → risks

三者完成并按计划顺序 merge
                    ↓
               EvidenceAgent
                    ↓
                ReportAgent
```

三个 Specialist 不互相聊天，也不等待另一个 Specialist 的自然语言消息。
它们读取各自受限的 SharedState View，独立调用 LLM/Skill/工具，最后由 Runtime
按计划顺序合并结构化产物。

| Agent | 本 Agent 任务 | 可能向 LLM 暴露的候选 Skill 元数据 | 确定性 pre-step | 实际公网 MCP |
|---|---|---|---|---|
| TechAgent | 技术证据、生产深度、JD 技术缺口 | `assess-technical-evidence`、`assess-production-engineering` | `calculate_jd_coverage` | 无 |
| ProjectAgent | 项目复杂度、个人贡献、技术决策、量化结果 | `ground-project-claims`；有显式外链时增加 `retrieve-public-candidate-evidence` | `locate_evidence` | 有显式外链且 MCP live 时暴露 `fetch`/`exa` |
| RiskAgent | 时间线、职责一致性、明确冲突与信息缺失 | `risk-pattern-detection`、`audit-claim-consistency` | `check_timeline` | 无 |

注意两点：

1. `AgentDefinition.skills` 是静态能力声明；`SkillManager.select_for()` 只根据
   Agent 权限、Run 类型和输入信号过滤出最多两个**允许向模型展示的候选 Skill**，
   不代表 Runtime 已决定使用这些 Skill。
2. 是否调用 Skill、调用哪个 Skill，由 LLM 阅读候选元数据后自主发起
   `load_skill(skill_id=...)`；不调用也合法。
3. `RiskAgent` 的 `AgentDefinition` 仍留有 `mcp_servers=("exa", "fetch")`，但生产
   `agentToolRouting.RiskAgent=[]`。最终工具目录以 live MCP route 为准，所以当前
   RiskAgent 实际没有公网 MCP。这是配置漂移，不能在面试中声称 Risk 会直接联网核验。

## 2. Skill 如何进入模型上下文

Skill 使用 progressive disclosure：

```text
Runtime 过滤并暴露 1～2 个候选 Skill 元数据
        ↓
第一次 LLM 调用只看到元数据
        ↓
LLM 根据当前证据缺口自主决定：不加载，或调用 load_skill(skill_id=...)
        ↓
Runtime 读取对应 SKILL.md
        ↓
下一轮模型获得完整业务指令
```

首次暴露示意：

```text
[可用技能] assess-technical-evidence（assess-technical-evidence@v1）：
根据具体 JD 和候选人可定位证据评估技术主张、深度与缺口……
  allowedTools: （未声明）
  → 需要时调用 load_skill(skill_id="assess-technical-evidence")
```

当前这六份 SKILL.md 的 frontmatter 都只声明了 `name` 和 `description`，没有
`allowed-tools`，所以元数据里会显示“未声明”。Agent 能否调用某工具仍由 Runtime
的 Provider 原生工具目录控制，不能从 Skill 文本中凭空获得工具权限。

## 3. TechAgent 的两个 Skill

TechAgent 在完整评估、JD 评估或存在有效 JD requirement 时会向 LLM 暴露两个
候选 Skill 的元数据。LLM 再自主决定是否加载以及加载哪一个。两者不是重复关系：
第一个判断“主张有没有证据以及证据达到什么深度”，第二个专门判断“这些证据是否
达到生产工程深度”。

### 3.1 assess-technical-evidence

它解决的问题是：简历写了某个技术名词，究竟只是提到、实际使用、参与设计、负责
生产运行，还是处理过故障？判断维度来自当前 JD，不使用固定技术清单。

具体例子：

```text
JD：要求 Redis 高可用和生产故障处理经验
简历：使用 Redis 实现热点数据缓存

Skill 判断：
- 可以证明 used；
- 不能直接升级为 designed / operated / troubleshot；
- Redis Sentinel、Cluster、容量、命中率、穿透治理均缺少证据；
- 应生成针对这些缺口的面试追问，而不是写“候选人不懂 Redis”。
```

它特别强调：知识库或技术文档只能说明框架能力，不能证明候选人做过；resume RAG
只是定位简历原文，不是第二份独立证据。

<details>
<summary>展开原始 SKILL.md：assess-technical-evidence</summary>

```markdown
---
name: assess-technical-evidence
description: 根据具体 JD 和候选人可定位证据评估技术主张、深度与缺口。需要技术栈评估、岗位相关评分、技术证据核验或生成技术追问时使用。
---

# Assess Technical Evidence

## 输入

接收 `normalizedJd`、`resumeClaims`、`projectClaims`、可选 `externalEvidence` 和 `experienceLevel`。

## 流程

1. 从 JD requirement 建立评估维度；不使用固定的通用技术清单。
2. 将每个技术主张绑定到简历或项目 source ref。
3. 区分 `mentioned | used | designed | operated | troubleshot | externally_supported`。
4. 根据岗位要求判断覆盖与深度，不从“使用过”推导“精通”。
5. 为证据不足但岗位关键的项目生成追问。

## 知识边界

- 框架/API 的通用能力以内部知识库召回为参考，不额外调用 100 份差异化压测中始终未被模型选择的文档 MCP。
- 技术文档只能说明框架能力，不能证明候选人真的做过；候选人事实仍必须绑定简历、项目或已核验外链。

## 交付

通过当前 Agent 的统一输出契约提交岗位维度、技术深度、优势、缺口和追问。不要定义、复述或包裹另一套 JSON Schema。

## 证据边界

- AI/ML 只在 JD 相关时进入评分，不作为所有岗位固定加分项。
- 外部资料只有真实工具成功返回且身份关联明确时使用。
- RAG chunk 只用于定位原文，不作为额外独立证明。
- 没有生产证据时标未知，不推断候选人没有能力。
```

</details>

### 3.2 assess-production-engineering

它把“会用框架”和“有生产工程能力”分开，重点检查：

- 架构约束与技术取舍；
- 可靠性、性能、安全、部署和可观测性；
- 规模、指标和观测窗口；
- 生产运行与故障处置；
- 团队系统能力和个人 ownership 的边界。

具体例子：

```text
简历：负责微服务系统性能优化，性能提升 50%

Skill 不会直接给高分，而是继续检查：
- 优化对象和瓶颈是什么？
- 50% 的基线、指标、单位和时间窗是什么？
- 候选人的个人动作是什么？
- 是否提供压测、监控或生产故障证据？
```

<details>
<summary>展开原始 SKILL.md：assess-production-engineering</summary>

```markdown
---
name: assess-production-engineering
description: 评估候选人在架构约束、可靠性、可观测性、性能、安全、部署和故障处置方面的生产工程证据。TechAgent 判断技术主张是否达到目标岗位所需的生产深度时使用。
---

# Assess Production Engineering

只评估目标 JD 需要的生产工程能力，不把技术名词数量当作深度。

## 工作流

1. 从 JD 中识别与架构、可靠性、性能、安全、交付或运维有关的要求。
2. 将候选人主张区分为开发使用、方案设计、生产运行和故障处置。
3. 查找约束、取舍、规模、指标、观测窗口和候选人个人动作；缺失时保持未知。
4. 区分团队系统能力与候选人可定位贡献，不从团队成果推导个人 ownership。
5. 对岗位关键但证据不足的能力提出可验证追问。

## 判断边界

- “使用过框架”不证明做过架构设计或生产治理。
- 没有 QPS、容量或故障数据时，不猜测系统规模。
- 技术文档说明产品能力，不证明候选人实际采用了该能力。
- 只使用简历、用户补充、内部检索命中和成功工具回执中的可定位证据。

## 交付

通过当前 Agent 的统一输出契约提交岗位相关的生产深度、证据缺口与追问。不要定义、复述或包裹另一套 JSON Schema。
```

</details>

## 4. ProjectAgent 的两个 Skill

ProjectAgent 有项目时会向 LLM 暴露 `ground-project-claims` 元数据；只有检测到
候选人显式 URL 时，Runtime 才把 `retrieve-public-candidate-evidence` 也加入候选
目录。LLM 仍然自主决定是否调用 `load_skill`，因此“已暴露两个”不等于“已加载两个”。

### 4.1 ground-project-claims

它把项目描述拆为：问题、行动、技术决策、个人贡献和结果，并检查量化指标有没有
基线、单位、时间窗和测量方式。

具体例子：

```text
原文：主导订单平台建设，性能提升 60%。

Skill 会拆成：
- “主导”对应哪些个人动作？
- 订单平台的复杂度和技术约束是什么？
- 60% 指响应时间、吞吐还是资源成本？
- 优化前后基线、测量时间窗和工具是什么？

缺少答案时保持未知或生成追问，不补写 QPS、团队人数和上线范围。
```

<details>
<summary>展开原始 SKILL.md：ground-project-claims</summary>

```markdown
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

## 交付

通过当前 Agent 的统一输出契约提交项目主张、复杂度、贡献边界、改写建议和待确认问题。不要定义、复述或包裹另一套 JSON Schema。

## 证据边界

- 不创造人数、QPS、提升比例、技术栈、上线范围或主导角色。
- 公司名气、公司规模和项目是否来自大厂不作为质量分。
- “参与”不自动等于低贡献，“主导”也必须有具体动作支持。
- 无法验证时保留未知，不写成造假风险。
```

</details>

### 4.2 retrieve-public-candidate-evidence

这个 Skill 不负责“随便上网搜候选人”，只处理候选人显式声明的公开 URL，并规定
URL 绑定、工具优先级、失败和限流语义。

#### MCP 不在 Skill 里面

`fetch`/`exa` 不是由 SKILL.md 封装或注册的。Runtime 独立完成 live MCP
`initialize → tools/list`，再把当前 ProjectAgent 有权使用且健康的 MCP function schema
放进 Provider 请求的 `tools[]`；Skill 元数据/正文只在 `messages` 中提供业务规则。

正常情况下不是“每到一个 Agent 就访问一次 MCP endpoint”。workflow 进程启动时创建
单例 `McpRegistry`，对各 MCP 服务执行一次 `initialize → tools/list`，把远端返回的
工具名、description、input/output schema 和协议版本保存在内存 `registry.tools` 中，
并保留可执行 `tools/call` 的 HTTP client 或 stdio 子进程。每个 Run 创建自己的
`ToolExecutor` 时只把这份 live catalog 注册到本 Run；每个 Agent 调用前也只在内存中
按 `agentToolRouting`、健康状态和当前输入信号过滤，不重新做网络 discovery。

```text
workflow容器启动
    ↓ initialize + tools/list（一次）
进程级 McpRegistry.tools（内存live catalog）
    ↓ 每个Run注册到自己的ToolExecutor.definitions（内存复制）
    ↓ 每个Agent按route/context过滤（内存操作）
Provider tools[]
    ↓ 只有模型真正tool_call时
MCP tools/call（真实endpoint/stdio调用）
```

健康 MCP client 不会周期性销毁和重复发现。当前限流状态按 **server** 管理，不按
tool 管理：同一 server 的任意一次 `tools/call` 被识别为 429/RATE_LIMITED，就会把该
server 标为 `RATE_LIMITED`，并在冷却期内隐藏它的全部工具。例如 Exa 的 search 被限流
会同时隐藏 Exa search 和 fetch；不会影响另一个独立 server 的 stdio fetch。服务处于
`DOWN`、`UNREACHABLE`，或 server 级限流冷却结束后，后续 Run/运维探测才会重新对该
降级服务执行 discovery；重探过程中其他健康 server 的 live catalog 仍原子保留。

```text
messages：Project system prompt + Skill元数据/正文 + 简历显式URL

tools[]：
- load_skill                         内部工具
- fetch_fetch                        映射到 MCP fetch.fetch
- exa_web_fetch_exa                  映射到 MCP exa.web_fetch_exa
- exa_web_search_exa                 映射到 MCP exa.web_search_exa
- emit_decision                      Runtime终态提交工具
```

因此 `load_skill` 不是 MCP 的权限开关：第一轮中，LLM 可能先调用 `load_skill` 再按
完整规则调用公网 MCP，也可能直接调用已经暴露的 MCP。工具是否出现取决于生产
`agentToolRouting`、实时 `tools/list`/健康状态以及当前输入是否存在外部 URL。

一次典型原生调用链是：

```text
LLM tool_call: fetch_fetch({"url":"候选人简历中声明的URL"})
    ↓ Runtime别名还原为 fetch.fetch
    ↓ 输入schema、预算、重复调用和域名白名单检查
    ↓ McpRegistry 调用 stdio MCP tools/call
    ↓ tool result 作为原生 tool message 返回LLM
    ↓ 成功且存在source URL时写入 artifacts.mcpEvidence
    ↓ EvidenceAgent校准后，ReportAgent才能把它用于候选人判断
```

当前硬校验能保证 MCP 必须 live、Agent route 允许、fetch 域名在白名单且没有 source
URL 的结果不能进入 `mcpEvidence`；但普通 fetch/Exa 尚未像 DeepWiki 一样在代码层
强制“模型参数中的 URL 必须与候选人原文声明 URL 完全相等”。这一点目前主要由
Project system prompt、Skill 规则和后续 Evidence 校准约束，属于仍可加强的 subject
binding 边界。

执行原则：

```text
候选人声明精确 URL
        ↓
优先 fetch.fetch 直接读取
        ↓
网络不可达且用户要求替代来源时才考虑 Exa
        ↓
404 = 这个精确页面不可用，不做同名搜索
429/5xx/超时 = not_checked，不等于 unsupported，更不等于造假
```

公网结果必须保留 source URL；抓取成功只证明页面内容可读，不自动证明账号归属、
作者身份或候选人个人贡献。

<details>
<summary>展开原始 SKILL.md：retrieve-public-candidate-evidence</summary>

```markdown
---
name: retrieve-public-candidate-evidence
description: 集中定义免密 Exa 和 fetch 对候选人声明 URL 的绑定、超时/限流与 not_checked 契约。仅在简历含显式外链、用户要求公网核验，或项目证据核验需要外部来源时使用。
---

# Retrieve Public Candidate Evidence

把公网证据检索收敛到统一契约，禁止把搜索结果直接写成候选人事实。

## 何时启用

- 简历或用户消息中存在**显式**个人主页 / 博客 / GitHub / Gitee / 作品集 URL。
- `evaluate-candidate-evidence` 产出的项目或技术 claim 需要外部核验。
- 用户明确要求“上网核验 / 打开这个链接”。

无显式 URL 且用户未要求公网核验时：**不要调用**本 Skill 关联工具。

## 工具优先级

1. **stdio fetch**（`fetch.fetch`）：对候选人声明的精确 URL 直接抓取。中国大陆 ECS 对目标可达时这是首选；其描述和参数 schema 必须来自实时 `tools/list`，不使用本地别名。
2. **Exa**（`exa.web_search_exa` / `exa.web_fetch_exa`）：只在用户明确要求发现替代公开来源，或精确 URL 因网络不可达而非 404 失败时兜底。免费 MCP 已限流时立即记 `not_checked`，禁止继续等待或重试。

生产 MCP 清单只允许免 OAuth、免 API Key 的服务；公开 GitHub 页通过 Exa 或白名单 fetch 核验。

### 中国大陆 ECS 路由

- GitHub 连通性按**运行时探测结果**处理，不能因机房地域直接假定可用或不可用。
- GitHub 先按运行时实测走白名单 fetch。明确 404 只说明候选人声明的 URL 当前不可用；网络不可达时才换到 Exa，Exa 限流则记 `not_checked`。
- Gitee / GitCode / CSDN / 掘金 / 知乎 / 博客园等候选人显式声明的国内链接，优先用白名单 fetch 直连；来源 URL 必须原样保留。
- 不得把同名 Gitee 镜像自动当成 GitHub 原仓库；只有简历显式声明或页面提供可验证的 canonical / mirror 关系时才允许绑定。

## URL 绑定与白名单

- 候选事实只允许绑定**候选人声明**的 URL / 标识。
- fetch 白名单域名：`github.com`、`gitee.com`、`juejin.cn`、`zhihu.com`、`csdn.net`、`cnblogs.com`、`medium.com`、`dev.to` 等（以运行时 `FETCH_ALLOWED_HOSTS` 为准）。
- 离白名单域名：本地拒绝，请求不得发出。
- 禁止无目标全网爬取。

## 超时 / 限流 / 失败契约

| 情况 | 输出 |
|------|------|
| 工具成功且带回 source URL | `toolStatus=success`，保留 url/title/publishedAt/provider |
| 超时、熔断、5xx | `toolStatus=failed`，claim 标 `not_checked` |
| 429 / RATE_LIMITED | `toolStatus=unavailable`，claim 标 `not_checked`，可降级到下一工具 |
| 空结果 | `emptyOrFailedResult=unavailable`，禁止合成证据 |

`not_checked` **绝不能**降级为 `unsupported` 或履历造假风险。

## 交付

通过当前 Agent 的统一输出契约提交已核验来源、身份绑定、未检查项和工具健康状态。不要定义、复述或包裹另一套 JSON Schema。

## 边界

- 所有公网结果必须带 `sourceUrl`；无 URL 的片段不得进入候选人证据台账。
- Microsoft Learn 框架文档不是候选人证据，不得经本 Skill 写入。
- ReportAgent 不得直接调用公网 MCP；只消费 Evidence 校准后的 ledger。
```

</details>

## 5. RiskAgent 的两个 Skill

RiskAgent 有时间线或执行风险/时间线任务时会向 LLM 暴露两个候选 Skill 元数据。
LLM 根据当前风险证据自主决定是否加载及加载哪个。确定性 `check_timeline` 先给出
时间段结果，已加载的 Skill 再约束模型如何解释这些结果。

### 5.1 risk-pattern-detection

它负责判断什么才是招聘决策相关风险，并明确禁止把未知信息、工具失败或受保护特征
写成负面结论。

具体例子：

```text
简历出现 2021.06-2023.08 公司A，同时出现 2022.01-2022.12 项目B。

不能仅凭日期重叠写“履历造假”。需要先判断：
- 项目B是否就是公司A内部项目；
- 两段文字的角色和实体是否互相不兼容；
- 是否存在两个可定位来源的直接冲突。

证据不足时输出“需要澄清”，而不是高风险结论。
```

空窗、跳槽次数、年龄、性别、地域、学校和公司名气都不能作为负面代理变量。

<details>
<summary>展开原始 SKILL.md：risk-pattern-detection</summary>

```markdown
---
name: risk-pattern-detection
description: 基于目标岗位和可定位证据检查时间线异常、职责边界与待核验风险。RiskAgent 需要判断风险严重度和岗位相关性时使用；不得把未知、未联网核验或受保护特征写成负面结论。
---

# Risk Pattern Detection

只报告与目标岗位和招聘决策直接相关、且能定位到证据的风险。风险识别不是人格推断，也不是背景偏见评分。

## 工作流

1. 将时间、角色、职责、项目和量化结果绑定到 source ref。
2. 先使用确定性时间线工具，再结合一致性审计结果判断风险。
3. 区分直接冲突、需要澄清、未检查和无风险信号。
4. 只有两个可定位来源直接冲突时，才允许降低履历可信度判断。
5. 其他状态只能保持未知或生成面试追问。

## 允许检查的信号

- 任职或教育时间重叠且简历没有解释。
- 职责或量化结果缺少个人贡献边界。
- 技术发布时间与声明使用时间明显不可能，并有成功工具回执支持。
- 一致性审计确认的岗位相关直接冲突。

## 禁止的代理变量

不得因年龄、性别、照片、婚育、民族、地域、学校名气、公司名气、空窗本身、跳槽次数本身或写作风格给负面风险。

## 失败边界

- RAG 无命中不证明主张为假。
- 公网 MCP 超时、限流、鉴权缺失或空结果一律保持未检查。
- 不把公司技术博客中的团队成果自动归于候选人个人。
- 任何会降低推荐等级的风险必须给出 source ref、岗位相关性和可复核理由。

## 交付

通过当前 Agent 的统一输出契约提交风险信号、证据与追问。不要定义、复述或包裹另一套 JSON Schema。
```

</details>

### 5.2 audit-claim-consistency

它不直接判定风险，而是比较同一实体在简历不同位置和已有 artifact 中的主张关系。
比较前会先统一日期粒度、百分比、数量级和单位。

它的四种核心裁决应该区分为：

```text
DIRECT_CONFLICT       两个来源不可同时成立，交给风险判断
COMPATIBLE_DIFFERENCE 口径不同但可以兼容
INSUFFICIENT_INFO     信息不足，生成澄清问题
NOT_CHECKED           依赖外部核验但工具未成功
```

具体例子：

```text
摘要：负责订单系统核心开发
项目段：参与订单模块开发

这可能是概括粒度不同，不自动构成冲突。

如果同一时间、同一项目一处写“项目负责人”，另一处明确写“仅负责测试执行”，
且两个来源都能定位，才可能形成直接冲突。
```

<details>
<summary>展开原始 SKILL.md：audit-claim-consistency</summary>

```markdown
---
name: audit-claim-consistency
description: 对简历不同位置及已生成 artifact 中的角色、时间、技术栈、指标和职责主张做一致性审计。RiskAgent 需要区分直接矛盾、口径差异与单纯信息缺失时使用。
---

# Audit Claim Consistency

一致性审计只识别主张之间的关系，不根据措辞风格推断诚信或人格。

## 工作流

1. 按项目、任职和教育实体归并重复主张，并保留每个 source ref。
2. 统一日期粒度、百分比、数量级和单位后再比较，避免把口径差异误判为冲突。
3. 检查同一实体的角色、职责、技术栈、指标、团队成果与个人动作是否直接矛盾。
4. 区分直接冲突、可兼容差异、信息不足和依赖外部核验但尚未检查。
5. 只把直接冲突交给风险判断；其余情况生成澄清问题或保持未知。

## 判断边界

- 缺少细节不是矛盾。
- 简历摘要比项目段落更概括，不自动构成不一致。
- 工具失败或未调用只能标记未检查，不能变成负面结论。
- 每个冲突必须同时给出至少两个可定位且互相不兼容的来源。

## 交付

通过当前 Agent 的统一输出契约提交冲突关系、来源和澄清方向。不要定义、复述或包裹另一套 JSON Schema。
```

</details>

## 6. 两个 Skill 为什么不合成一个

| Agent | Skill 1 | Skill 2 | 分开的原因 |
|---|---|---|---|
| Tech | 技术主张证据与岗位覆盖 | 生产工程深度 | “做过某技术”和“在生产中设计、运行、排障”不是一个判断 |
| Project | 项目事实、ownership、指标 | 显式 URL 公网核验 | 内部项目分析不应默认触发外网；公网失败语义也需要独立约束 |
| Risk | 风险严重度与公平边界 | 主张间一致性关系 | 一致性审计先回答“是否矛盾”，风险判断再回答“是否影响招聘决策” |

Skill 分开后仍由同一个 Agent 产出一个统一 AgentOutput，不会产生六份互相冲突的
输出，也不会新增六个图节点。

## 7. 三个 Agent 的结果如何进入后续阶段

```text
TechAgent.output.claims(section=technical_findings)
    → artifacts.technicalFindings

ProjectAgent.output.claims(section=project_findings)
    → artifacts.projectFindings

RiskAgent.output.claims(section=risks)
    → artifacts.risks
```

并行组 merge 后，EvidenceAgent 从上述三个 artifact 收集可识别的 `text/finding/detail`
主张，用简历、JD 和成功外部工具回执生成支持、不支持、未核验和冲突裁决；ReportAgent
最后消费这些产物生成唯一报告。

## 8. 面试时的准确说法

> 第一并行组不是三个通用 Prompt 的复制。Tech、Project、Risk 共享同一候选人输入，
> 但通过不同 system prompt、`task_prompt`、SharedState Read Map、确定性 pre-step、
> Provider 工具目录和按需加载 Skill 形成职责隔离。Skill 首轮只暴露 name/description，
> 模型需要时调用 `load_skill` 获取完整 SKILL.md；它不会因为 Skill 文本提到某个工具就
> 自动获得权限。三个 Agent 的结构化产物在组边界按计划顺序合并，再由 EvidenceAgent
> 做跨产物证据核验。

## 9. 当前必须诚实说明的设计债

1. 六份 Skill 的 frontmatter 没有 `allowed-tools`，初始元数据均显示“未声明”；工具权限
   实际由 Runtime 独立控制。
2. `AgentDefinition.skills` 和 `SkillManager.select_for()` 同时表达 Skill 关系；最终
   可见候选目录以 `select_for()` 为准，但真正是否使用仍由 LLM 的 `load_skill`
   决策，当前存在双处资格配置。
3. RiskAgent 的 `mcp_servers` 声明和生产 `agentToolRouting` 不一致；实际生产工具面没有
   Risk 公网 MCP。
4. Skill 要求“统一输出契约”是正确的，但当前 Specialist `claims/evidence` 仍是松散
   `Dict[str, Any]`，尚未形成严格的 per-Agent Schema。

这些限制不会改变六个 Skill 的业务职责，但文档和面试描述不能把声明配置当成实际
Provider 请求。
