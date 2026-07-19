# 秋招面试问题与答案（本项目）

## Q1：同会话为何必须串行？
A：Shared State、摘要与证据链按时间序演化；并行会导致互相覆盖与取消语义混乱。用 Redis 锁+队列保证多实例下仍串行。

## Q2：COLLECT 与 INTERRUPT 区别？
A：COLLECT 不打断当前 Run，结束后合并补充消息；INTERRUPT 协作式取消 LLM/Tool/Sandbox，保留已完成副作用 Trace，再开新 Run。

## Q3：为何不用进程内锁？
A：多实例与重启会失效；Redis 分布式锁+DB 状态才能正确恢复。

## Q4：Sandbox 解决什么？
A：把 PDF/规则核验与主服务隔离，network=none + 资源限额，避免任意代码执行风险。

## Q5：策略学习是训练大模型吗？
A：不是。是 Agent 外层 PolicyBundle 的 epsilon-greedy 选择，基于 HR 反馈与 Benchmark Reward。

## Q6：Context 压缩如何不丢 Tool 配对？
A：压缩前做一致性检查，未闭合 Tool Call 与对应 Result 必须同时保留。

## Q7：证据如何防编造？
A：EvidenceAgent + Sandbox verify；无证据结论标记不确定；Benchmark 惩罚 unsupported claims。
