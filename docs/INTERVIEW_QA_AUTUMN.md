# 秋招面试问题与答案（本项目）

## Q1：Java 控制面与 LangGraph 如何分工？
A：Java+MySQL/Redis负责业务 Run 的队列、状态机、许可与最终结果；LangGraph只
负责 Python Agent 的节点编排，节点 checkpoint 存 PostgreSQL，`thread_id=runId`。
两者通过同一个 runId 和事件回调对齐，不让图框架接管业务队列。

## Q2：同会话为何必须串行？
A：Shared State、摘要与证据链按时间序演化；并行会互相覆盖且取消语义混乱。
用 Redis 会话锁（租约+watchdog 续租）+ MySQL FIFO 队列保证多实例下仍串行。

## Q3：COLLECT 与 INTERRUPT 区别？
A：COLLECT 不打断当前 Run，补充消息合并进待执行 Run；INTERRUPT 协作式取消
LLM/Tool，保留已完成副作用 Trace，被取代的排队消息折叠进新 Run。

## Q4：PAUSE 的精确语义是什么？
A：Agent 组边界的协作式暂停：图进入 interrupt，PostgreSQL持久化完整节点状态；
Java 同时保存 RunExecutionSnapshot 副本。恢复以相同 `thread_id=runId` 执行
`Command(resume=...)`，从下一个未完成节点继续。不承诺冻结正在输出的 token。

## Q5：Specialist 并行怎么保证不打架？
A：Coordinator 按依赖表分组，LangGraph 用 `Send` fan-out Tech/Project/Risk，
各自只读状态视图；Reducer收集输出后由 merge 节点串行写黑板，同键冲突写 conflicts
而非覆盖。真实效果：完整评估 LLM 调用 18→8 次、时延 140s→46s。

## Q6：ReportAgent 失败怎么办？
A：不重排（重试同样失败只烧预算），直接基于黑板已有结果生成显式标注的降级
输出，状态如实标 PARTIAL_SUCCESS——降级永不伪装成功。长报告溢出 JSON 包装
时直接接受原始 markdown，报告本身就是交付物。

## Q7：Context 压缩如何不丢 Tool 配对？
A：Tool Call/Result 按 toolCallId 一一配对校验（孤儿 call 或孤儿 result 都
违规，数量恰好相等骗不过）；压缩以 call/result 对为单位保留或整体丢弃。
Token 估算区分中英文并用真实 API usage 在线校准。

## Q8：策略学习是训练大模型吗？
A：不是。是 Policy Optimization Lab（无 GPU）：生产决策 champion-only
（ONLINE_SELECTION）；epsilon-greedy/Thompson 只在 shadow/lab；离线
OFFLINE_SEARCH 是有界配置进化（非完整 GEPA）；MODEL_WEIGHTS unchanged。
基于 HR 反馈与真实 E2E Benchmark 的 Reward。Benchmark 分两层：Contract（离线确定
性回归，不出质量结论）与 Real E2E（真实 LangGraph/LLM/Token，唯一能选 Champion）。

## Q9：Benchmark 怎么防标签泄漏？
A：mustFind/mustNotClaim/expectedRisk 只进评估器进程，从不进入会话内容、
Prompt、工具参数、Memory 或 Shared State；固定策略用 forcedPolicyId 显式记录
FORCED 不污染探索统计；成本由真实 token 乘官方单价计算，无一个模拟数字。

## Q10：为什么 checkpoint 单独使用 PostgreSQL？
A：LangGraph官方 PostgresSaver直接持久化 channel values、pending writes 和节点
位置，能恢复并行 Send 中已完成的分支；MySQL仍保存业务状态与结果，避免把框架
内部 checkpoint schema 塞进业务表。部署时 Checkpointer不可用会失败关闭。
