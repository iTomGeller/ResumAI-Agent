# ResumAI 对话式简历评估 Agent 架构

```text
Vue Frontend（对话 / 运行进度 / 停止・暂停・恢复）
  → Spring Boot Conversation & Run Control Plane
      MySQL：Conversation / Message / Revision / Run / Event / Snapshot /
             Memory / Policy / Reward / Benchmark
      Redis：全局并发信号量、会话锁（租约）、取消信号
  → Python Agent Runtime（唯一执行入口 /agent/runs）
      Coordinator（规则优先 + 能力目录 LLM 精化 + 依赖拓扑 + 并行分组）
      LangGraph StateGraph（PostgreSQL Checkpointer, thread_id=runId）
      Send Parallel Specialists（Tech ∥ Project ∥ Risk）→ Reducer → Evidence → Report
      Command 动态 Replan（预算 / LoopGuard / 降级）
      Tool Gateway / Memory / Context（toolCallId 配对压缩）
  → Runtime Events + 最终结果回调 Java → SSE 推送前端（断线回放）
```

关联 ID：userId, conversationId, runId, traceId, revision

## 职责边界
- **Java 是唯一持久事实源**：队列、串行/并发、取消、暂停恢复、恢复扫描、
  可见结果与 fencing 全在 Java + MySQL/Redis。
- **Python 图状态持久化**：LangGraph 节点 checkpoint 存 PostgreSQL；MySQL 保留
  Java 控制面的 Run 状态与 executionSnapshot 副本。
- 旧 `RunExecutor` 通过 `LANGGRAPH_RUNTIME_ENABLED=false` 保留为回退路径。

## 内部 API（Docker 网络 + 内部 Token）
POST /agent/runs · GET /agent/runs/{id} · POST /agent/runs/{id}/cancel
POST /agent/runs/{id}/pause · POST /agent/runs/{id}/resume
POST /conversation/turns/resolve

## 业务 Agent
ResumeParser（确定性内置工具解析）、JDAnalysis、Tech、Project、Risk、
Evidence（核验+冲突）、Report（显式终点）、ResumeOptimize、InterviewQuestion、
Coordinator（规划专用，不占执行位）。
