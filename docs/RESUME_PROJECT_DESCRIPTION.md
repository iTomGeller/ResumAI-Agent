# 秋招简历项目描述（准确版）

实现对话级 Agent Run 调度：Java 控制面以 MySQL FIFO 队列 + Redis 租约锁支持
多用户并行、同会话严格串行、运行中补充（COLLECT）与打断（INTERRUPT），并将
暂停/恢复建模为安全边界执行快照，LangGraph PostgreSQL Checkpointer以
`thread_id=runId` 持久化节点位置，恢复时同一 runId/traceId
续跑且绝不重放已完成的非幂等动作。

使用 LangGraph StateGraph 编排核心执行链：Coordinator 规则优先 + 能力目录约束
的 LLM 精化动态规划，通过 Send并行技术/项目/风险 Specialist，Reducer统一收集，
Command完成组间 dispatch，证据 Agent 核验并限制报告证据边界，报告 Agent 显式收尾；
完整评估 LLM 调用从 18 次降至 8 次、时延从约 140 秒降至约 46 秒（真实指标）。

实现 Policy Optimization Lab（无 GPU）：将 Agent 组合、并行开关、Prompt/
Skill 版本、Tool/Context 预算与证据核验规则抽象为 PolicyBundle；生产决策
champion-only，bandit（epsilon-greedy/Thompson）仅 shadow/lab；离线为有界
配置进化（非完整 GEPA）。按 HR 反馈与真实 E2E Benchmark（真实 LLM 调用次数、
真实 Token 成本、评估标签与执行链严格隔离）持续改进策略；MODEL_WEIGHTS unchanged。
