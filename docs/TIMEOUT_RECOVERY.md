# 超时、失败与恢复

## LLM
connect/read/total timeout；最多重试 2 次；指数退避+jitter；cancellation；
circuit breaker；仅重试临时网络/限流/5xx。

## Tool
幂等只读可重试；非幂等不自动重复。记录 toolCallId/status/retry/heartbeat。

## Watchdog
状态卡住、LLM/Tool 无进度、锁泄漏、总时限、队列未消费 → 取消并释放锁、写 Trace、SSE 通知。

关键：`workflow/app/runtime/llm.py`、`RunSchedulerService`、`RunLifecycleService`
