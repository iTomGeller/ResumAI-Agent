# 秋招面试问题与答案（本项目）

## Q1：为什么删掉旧 Runtime 而不是留兼容层？
A：两套 Run 状态机（进程内 registry + LangGraph checkpoint）意味着取消、恢复、
迟到结果各有两条路径，任何一条漏了都会造成状态分裂。收敛后 Java+MySQL/Redis
是唯一事实源，Python 只持有活动 asyncio Task 句柄；旧 `/workflow/runs`、
`/execute` 和 checkpoint PostgreSQL 彻底删除，CI 里有 grep 门禁防止回流。

## Q2：同会话为何必须串行？
A：Shared State、摘要与证据链按时间序演化；并行会互相覆盖且取消语义混乱。
用 Redis 会话锁（租约+watchdog 续租）+ MySQL FIFO 队列保证多实例下仍串行。

## Q3：COLLECT 与 INTERRUPT 区别？
A：COLLECT 不打断当前 Run，补充消息合并进待执行 Run；INTERRUPT 协作式取消
LLM/Tool/Sandbox，保留已完成副作用 Trace，被取代的排队消息折叠进新 Run。

## Q4：PAUSE 的精确语义是什么？
A：Agent 组边界的协作式暂停：executor 在组间检查 pause 事件，导出
RunExecutionSnapshot（plan/已完成 Agent/SharedState/预算/LoopGuard/工具台账）
经回调存入 MySQL。恢复用同一 runId/traceId/revision 重派，已完成 Agent 与
Tool Call 绝不重跑（有测试固化）。不承诺冻结正在输出的 token。PAUSED 释放
全局并发额度但保留会话锁，TTL 到期自动取消防止永久占用。

## Q5：Specialist 并行怎么保证不打架？
A：Coordinator 按依赖表分组（Tech/Project/Risk 读取不相交的黑板区），组内
asyncio.gather，各自只读状态视图，输出组后串行合并；同键冲突写 conflicts
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
性回归，不出质量结论）与 Real E2E（真实 LLM/Token/Sandbox，唯一能选 Champion）。

## Q9：Benchmark 怎么防标签泄漏？
A：mustFind/mustNotClaim/expectedRisk 只进评估器进程，从不进入会话内容、
Prompt、工具参数、Memory 或 Shared State；固定策略用 forcedPolicyId 显式记录
FORCED 不污染探索统计；成本由真实 token 乘官方单价计算，无一个模拟数字。

## Q10：Sandbox 的威胁模型？
A：恶意简历在 network=none/只读/非 root/限额的一次性容器里解析；镜像按部署
Git SHA 固定禁止 latest；调用方永远无法指定镜像/命令/挂载/网络；Manager 持有
Docker Socket 是单机形态的已知边界，靠内网隔离+内部 token+参数白名单缓解。
