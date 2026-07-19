# 秋招简历项目描述（准确版）

实现对话级 Agent Run 调度，通过 Redis 分布式队列和 Conversation 串行锁支持多用户并行、同会话顺序执行及运行中断，解决并发上下文竞争和长任务无法取消问题。

重构简历评估 Agent Runtime，由 Coordinator 根据用户问题、上下文和历史反馈动态选择技术、项目、风险、证据和报告 Agent，并通过 Shared State 实现证据共享、冲突记录及可追踪报告生成。

构建任务级 Docker Sandbox，隔离 PDF 解析、时间线检查、JD 覆盖率计算和报告证据核验，通过无网络、只读根文件系统、非 Root、资源限额、超时和 TTL 清理限制工具执行风险。

实现基于 HR 反馈的无 GPU Agent 策略学习，将 Agent 组合、Prompt、Skill、Tool Budget、Memory 检索和证据核验规则抽象为 PolicyBundle，通过 epsilon-greedy 和 Sandbox Replay Benchmark 持续选择表现更优的策略。
