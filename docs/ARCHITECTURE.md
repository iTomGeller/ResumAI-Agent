# ResumAI 对话式简历评估 Agent 架构

```text
Vue Frontend（对话 / 运行进度 / 停止・暂停・恢复）
  → Spring Boot Conversation & Run Control Plane
      MySQL：Conversation / Message / Revision / Run / Event / Snapshot /
             Memory / Policy / Reward / Benchmark
      Redis：全局并发信号量、会话锁（租约）、取消信号
  → Python Agent Runtime（唯一执行入口 /agent/runs）
      Coordinator（规则优先 + 能力目录 LLM 精化 + 依赖拓扑 + 并行分组）
      RunExecutor（预算 / LoopGuard / 暂停快照 / 降级）
      Parallel Specialists（Tech ∥ Project ∥ Risk）→ Evidence → Report
      Tool Gateway / Memory / Context（toolCallId 配对压缩）
  → Sandbox Manager → Ephemeral Docker Worker（Git SHA 镜像, network=none）
  → Runtime Events + 最终结果回调 Java → SSE 推送前端（断线回放）
```

关联 ID：userId, conversationId, runId, traceId, revision

## 职责边界
- **Java 是唯一持久事实源**：队列、串行/并发、取消、暂停恢复、恢复扫描、
  可见结果与 fencing 全在 Java + MySQL/Redis。
- **Python 无持久状态机**：只持有正在执行的 asyncio Task 句柄（真实取消用），
  重启后一切凭 Java 恢复；PAUSED 快照存 MySQL。
- 旧的图执行 Runtime（含 `/workflow/runs`、`/execute`、checkpoint PostgreSQL）
  已彻底删除，无兼容层。

## 内部 API（Docker 网络 + 内部 Token）
POST /agent/runs · GET /agent/runs/{id} · POST /agent/runs/{id}/cancel
POST /agent/runs/{id}/pause · POST /agent/runs/{id}/resume
POST /conversation/turns/resolve

## 业务 Agent
ResumeParser（确定性 Sandbox 解析）、JDAnalysis、Tech、Project、Risk、
Evidence（核验+冲突）、Report（显式终点）、ResumeOptimize、InterviewQuestion、
Coordinator（规划专用，不占执行位）。
