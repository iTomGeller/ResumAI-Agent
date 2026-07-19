# 对话队列与并发

## 规则
- 不同 Conversation：并行
- 同一 Conversation：严格串行（并发=1）
- 全局 Run 最大并发：默认 4（`resumai.agent-run.max-global-concurrent`）

## 实现
- MySQL 持久队列（`agent_run` QUEUED 行按 created_at FIFO）+ Redis 许可
  （全局信号量 + 会话锁，带租约与 watchdog 续租）：`RunPermitService` / `RunQueueService` / `RunSchedulerService`
- Run 状态机：QUEUED → STARTING → RUNNING ⇄ WAITING_LLM/TOOL/SANDBOX
  → PAUSING → PAUSED → RESUMING → RUNNING
  → CANCELLING → CANCELLED / SUCCEEDED / PARTIAL_SUCCESS / FAILED / TIMED_OUT
- 启动恢复：QUEUED 保留；孤儿 RUNNING 标记 FAILED；PAUSING 有快照则收敛为
  PAUSED；PAUSED 跨重启保留（快照在 MySQL）；CANCELLING 宽限期后强制关闭

## queueMode
- `collect`：当前 Run 继续，补充消息合并进待执行 Run（顺序保留）
- `interrupt`：当前 Run CANCELLING → Python 取消 asyncio Task/LLM/Tool/Sandbox
  → CANCELLED → 队列中被取代的消息折叠进新 Run

## PAUSE / RESUME
- `POST /api/runs/{id}/pause`：CAS → PAUSING → Python 在 Agent 组边界导出
  RunExecutionSnapshot → 状态 PAUSED（释放全局额度、保留会话锁）
- `POST /api/runs/{id}/resume`：重取全局许可 → RESUMING → 携带快照重派
  `/agent/runs/{id}/resume` → 已完成 Agent/Tool 绝不重跑
- Pause TTL（默认 2 小时）到期自动 CANCELLED，避免永久占用会话

## 关键代码
- `backend/.../service/run/RunQueueService.java`（含 resume_task 桥接 enqueueTaskRun）
- `backend/.../service/run/RunSchedulerService.java`（派发/watchdog/恢复）
- `backend/.../service/run/RunLifecycleService.java`（状态机与 fencing）
