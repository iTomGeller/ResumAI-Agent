# 超时、失败与恢复

## LLM（`workflow/app/runtime/llm.py`）
connect/read/total timeout；最多 2 次安全重试；指数退避+jitter；熔断器；
可选 fallback 模型；仅重试临时网络/限流/5xx；400/413/401/403 等确定性错误
不盲目重试；真实 usage 回填 token 计数并校准上下文估算。取消通过 asyncio
任务取消传播——httpx 请求在 await 点被真实中断（AsyncClient 上下文关闭连接）。

## Tool（`workflow/app/runtime/tools.py`）
超时；只读幂等工具才自动重试；结构化错误分类；tool.started/progress/
completed/failed 事件带 toolCallId/idempotencyKey/sideEffectLevel；
台账（ledger）随暂停快照迁移，恢复后已完成调用不重复执行。

## 终端 Agent 失败
ReportAgent 失败不重排（避免同因重试烧预算），直接生成显式标注的降级答案，
Run 状态 PARTIAL_SUCCESS。

## Watchdog（`RunSchedulerService`）
- Run 超总时限 → TIMED_OUT（取消传播）
- CANCELLING 超宽限 → 强制 CANCELLED
- PAUSING 无快照超宽限 → 回退 RUNNING
- PAUSED 超 TTL（默认 2h）→ 自动 CANCELLED（会话锁释放）
- STARTING 卡死 → FAILED
- 活动 Run 心跳续租 Redis 许可；PAUSED 只续会话锁

## 重启恢复
QUEUED 保留；runtime 仍活跃的 Run 被收养；孤儿 RUNNING → FAILED；
PAUSING 已有快照 → PAUSED；PAUSED 凭 PostgreSQL LangGraph checkpoint
跨重启恢复，MySQL保留控制面快照副本。
所有异常路径最终收敛为终态，不存在永久 RUNNING。
