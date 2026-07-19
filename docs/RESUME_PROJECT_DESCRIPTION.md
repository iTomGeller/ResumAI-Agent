# 秋招简历项目描述（准确版）

实现对话级 Agent Run 调度：Java 控制面以 MySQL FIFO 队列 + Redis 租约锁支持
多用户并行、同会话严格串行、运行中补充（COLLECT）与打断（INTERRUPT），并将
暂停/恢复建模为安全边界执行快照（MySQL 持久化），恢复时同一 runId/traceId
续跑且绝不重放已完成的非幂等动作。

重构简历评估 Agent Runtime 为唯一执行链：Coordinator 规则优先 + 能力目录约束
的 LLM 精化动态规划，技术/项目/风险 Specialist 以只读状态视图并行执行后统一
合并，证据 Agent 核验、报告 Agent 显式收尾；删除旧图执行 Runtime 与双状态机，
完整评估 LLM 调用从 18 次降至 8 次、时延从约 140 秒降至约 46 秒（真实指标）。

构建任务级 Docker Sandbox：隔离 PDF 解析、时间线检查、JD 覆盖率与报告证据
核验，无网络、只读根文件系统、非 Root、资源限额、TTL 清理，Worker 镜像按部署
Git SHA 固定，调用方无法指定镜像/命令/挂载/网络。

实现基于反馈的无 GPU Agent 外层策略学习：将 Agent 组合、并行开关、Prompt/
Skill 版本、Tool/Context 预算与证据核验规则抽象为 PolicyBundle，以
epsilon-greedy/Thompson 按 HR 反馈与真实 E2E Benchmark（真实 LLM 调用次数、
真实 Token 成本、评估标签与执行链严格隔离）持续选择更优策略。
