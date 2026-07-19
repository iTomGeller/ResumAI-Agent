# 测试报告（ECS 真实执行，2026-07-19）

执行环境：阿里云 ECS 4C16G（Ubuntu, Docker 29.1.3, Compose 2.40.3），项目目录 `/opt/resumai-src`，Compose project `resumai`。

## 单元 / 集成测试

- Java 后端（JDK21，ECS 上 `mvn test`）：8 个测试类 23 用例全部通过
  （McpToolRegistry 2、DbMigrationRunner 3、BuiltinMcpServer 3、ConversationIntentClassifier 5、
  ExternalProfileService 2、MemoryRedaction 2、RunTypeClassifier 2、MarkdownTextUtil 4）。
- Python Workflow：pytest 作为 workflow 镜像构建门禁在 ECS Docker build 内执行通过
  （runtime core / executor / loop guard / sandbox tools / conversation / run control / tool registry / mcp registry / policy benchmark）。

## 端到端（真实 LLM + Sandbox）

- 完整评估 run `run-c1440039`：SUCCEEDED，policy=deep_analysis，
  6 个 Agent（ResumeParser→JDAnalysis→Tech→Project→Risk→Evidence），
  18 次 LLM 调用、4 次 Sandbox 工具调用（0 失败），耗时 166s，回答含证据行引用。
- SSE：`/sse/runs/{runId}` 断线重连按 `Last-Event-ID` 回放 run_event（验证含 run.queued/run.started/llm.*/tool.*/sandbox.*/run.completed）。

## 并发与队列

- 不同 Conversation 并行：conv-A 与 conv-B 同时进入 WAITING_LLM。通过。
- 同一 Conversation 串行：第二条消息 queuePosition=1 排队。通过。
- COLLECT：运行中补充消息 RUN_QUEUED 合并等待（run-9e201bc6）。通过。
- INTERRUPT：运行中停止 → run-3b86e687 状态 CANCELLED，取消原因“用户在对话中要求停止”，Python runtime task 取消。通过。

## 超时 / 失败 / 恢复

- LLM 客户端：重试上限 2、指数退避（AgentRuntimeClient 401 场景验证不盲目重试 4xx）。
- Sandbox 工具超时路径：修复前 4 次 TIMED_OUT 有完整 tool.failed / sandbox_execution 记录，Agent 降级输出而非挂起。
- 服务重启恢复：restart 后 WAITING_LLM 孤儿 run 被启动恢复扫描标记 FAILED，无永久卡死。

## Sandbox 安全

- worker 容器：`printenv | grep -cE 'PASSWORD|API_KEY|TOKEN|SECRET'` = 0，uid=65534，
  network=none、read-only rootfs、cap-drop ALL、no-new-privileges、tmpfs 配额。通过。
- 生产链路 sandbox_execution：11 SUCCEEDED（修复后全部成功，parse_resume 299ms）。

## Memory / Policy Learning

- memory_entry 按 scope 隔离：FAILURE(GLOBAL) 6、EPISODIC(CONVERSATION) 11、HR_FEEDBACK 2、CONVERSATION 3。
- HR 反馈 → reward=0.8895 → policy_statistics(deep_analysis, full_evaluation) 更新。
- policy_selection 记录 EXPLOIT/EXPLORE 与 epsilon。

## Benchmark（ECS Sandbox Replay，bench-20260719-142845-2a636a72）

19 用例 × 7 策略 = 133 结果，全部 SUCCEEDED，Champion = **strict_evidence**（总 Reward 0.353）。
详见 `reports/benchmark/`（JSON/CSV/Markdown）。

## Volume 持久化

- 重启前后：conversation_session 7→7、agent_run 11→11、resume_task 203→203。
- resumai-mysql-data / resumai-redis-data 等原 named volume 全程未改名未替换；
  部署前 mysqldump 备份于 `/root/resumai-backups/<stamp>/`。

## 部署期间发现并修复的缺陷

1. `application.yml` 缩进错误使 `workflow/task-queue/agent-run/policy/memory` 挂在 `langfuse` 前缀下，
   导致内部 token 绑定失效（发出 change-me，HTTP 401）。已修复。
2. Docker 不向 detached 容器转发 attach 流 EOF，sandbox worker 阻塞 stdin 读满 90s 超时。
   worker 改为增量解析完整 JSON 文档。已修复（299ms 成功）。
3. `requirements.lock` 含无平台标记的 pywin32，Linux 构建失败。已加 `sys_platform == "win32"`。
4. `.dockerignore` 排除了 ECS 构建所需的 `target/jar` 与 `dist/`。已改为白名单。
