# 对话队列与并发

## 规则
- 不同 Conversation：并行
- 同一 Conversation：严格串行（并发=1）
- 全局 Run 最大并发：默认 4（`resumai.run.global-max-concurrent`）

## 实现
- Redis 队列 + Conversation 分布式锁：`RunPermitService` / `RunQueueService`
- Run 状态机：QUEUED → STARTING → RUNNING → WAITING_* → SUCCEEDED/FAILED/CANCELLED/TIMED_OUT
- 启动恢复：扫描异常 RUNNING，标记 FAILED/TIMED_OUT 或重新入队（`RunSchedulerService`）

## queueMode
- `collect`：当前 Run 继续，补充消息合并后创建下一 Run
- `interrupt`：CANCELLING → 取消 LLM/Tool/Sandbox → CANCELLED → 用最新消息创建新 Run

## 关键代码
- `backend/.../service/run/RunQueueService.java`
- `backend/.../service/run/RunPermitService.java`
- `backend/.../service/ConversationService.java`
