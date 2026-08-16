# 为什么自研统一 Agent Runtime，而不是 LangGraph

> 面试速答版在文末。本文写清楚决策依据、代价与边界——这是一个有取舍的工程决策，
> 不是"不会用框架"或"为了造轮子"。

## 1. 决策背景

本系统曾经用 LangGraph 实现评估工作流（固定 6-Phase DAG + Postgres checkpointer）。
重构时彻底移除，替换为自研统一 Runtime（Java 控制面 + Python 执行面）。
触发重构的不是框架本身的缺陷，而是我们的四个硬需求与 LangGraph 的模型不匹配。

## 2. 四个硬需求与不匹配点

### 2.1 状态主权必须在 Java + MySQL（跨进程、跨语言、可恢复）

- 我们的运行状态机（QUEUED → STARTING → RUNNING → WAITING_* → PAUSING → PAUSED →
  RESUMING → 终态）由 Java 用 **数据库 CAS**（`UPDATE ... WHERE status IN (...)`）驱动，
  配合 Redis 分布式 permit 实现"会话内严格串行、跨会话并行"的排队语义。
- LangGraph 的 checkpointer 是 **Python 进程内的图状态持久化**。硬套等于两套状态双写：
  Java 的 run 状态与 LangGraph 的 thread 状态互相追赶，取消/暂停/超时的一致性边界
  会碎在两个系统之间（我们真实踩过：取消信号到达时 LangGraph 节点仍在跑，
  旧运行时需要额外的 fencing 层）。
- 自研后：Python 端 **没有任何持久状态**（重启即弃），暂停快照通过结果回调交给 Java
  落 MySQL，watchdog 与恢复逻辑全部单点在 Java——一个事实源。

### 2.2 预算必须在每次调用前强制（token 级 harness 要求）

- 预算检查（maxLlmCalls / maxTotalTokens / maxCostCny / per-agent quota）发生在
  **LLM/工具调用发出前**，且成本核算要感知 DeepSeek 前缀缓存命中价差。
- LangGraph 的干预点是节点粒度（before/after node）。一个节点内部多轮 LLM 调用的
  token 级前置拦截、repair 调用计数、循环护卫（重复工具签名/无新信息检测）
  都要穿透到调用点——等于把框架的执行循环整个替换掉，框架只剩一层壳。

### 2.3 我们的计划在 Run 开始时生成，运行期间保持不变

- Coordinator 在开始时根据 artifact 依赖、输入信号和预算生成计划与并行分组。
- 运行期间不 Replan、不 handoff、不插入 Agent；LLM/工具调用有限重试，Agent 最终失败则降级。
- ReportAgent 直接结合原始材料限制最终报告的证据边界，不修改执行拓扑。

### 2.4 排队/打断/合并语义是产品核心，不是框架附属品

- COLLECT（补充消息合并进待跑 run）、INTERRUPT（取消当前 + 折叠队列）、
  plan-approval（规划后暂停等确认）这些语义都长在 Java 队列层，
  与 LangGraph 的 thread/run 模型没有对应物。

## 3. 承认的代价（自研不是免费的）

| 自研要自己扛的 | 我们的对策 |
|---|---|
| checkpointer / 快照恢复 | 组边界快照 + Java 落库 + 断点重试（已测） |
| 事件溯源 / trace | run_event 表 + SSE replay（Last-Event-ID 续传） |
| 循环护卫 / 预算 | LoopGuard 六类检测 + 调用前预算栅栏 |
| 结构化输出保障 | function calling → json_object → 抽取 → pydantic → repair 五层 |
| 生态集成（工具/模型） | 工具面窄（评估域固定工具 + MCP），不需要框架的连接器生态 |
| 框架演进红利 | 关注 LangGraph 的设计（如 interrupt/Command 模式）择优吸收 |

## 4. 什么时候会选 LangGraph

如实回答：如果是**新做一个通用 agent 产品**、单 Python 进程、需要快速接大量
第三方工具、团队没有强排队/审计需求——LangGraph 是更好的起点（生产成熟度和
生态是它的强项）。我们的场景是"评估域固定工具 + 强控制面 + 双语言架构"，
自研的控制收益大于框架的生态收益。

## 5. 面试速答（30 秒版）

"我们最初就是 LangGraph。重构删掉它有四个原因：一是状态主权必须在 Java+MySQL
（跨语言 CAS 状态机 + 分布式排队），LangGraph 的 Python 进程内 checkpointer 会造成
双写；二是预算要在每次 LLM 调用前做 token 级前置拦截，节点粒度 hook 不够；
三是我们的执行图是 Coordinator 每次动态生成、运行中还会重规划和 handoff，
静态图 DSL 反而多一层间接；四是排队/打断/合并这些产品语义在框架里没有对应物。
代价是 checkpointer、事件溯源、循环护卫都要自己写——这些我们都补齐并有测试覆盖。
如果是单进程通用 agent 产品我会选 LangGraph，这是场景决策不是能力偏好。"
