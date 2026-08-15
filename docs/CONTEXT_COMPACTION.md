# Context 压缩

预算：system/policy/skill/recentMessage/memory/toolResult/reservedOutput。

Token 估算：CJK ≈0.7 token/字符、ASCII ≈1/3.6，并用真实 DeepSeek usage
指数滑动校准（`app/runtime/context.py: calibrate`），系数下限 1.0 保守。

达到模型窗口 70%–80%（compactAtRatio）触发压缩。保留：最新用户请求、
当前目标、取消/限制、未完成任务、未闭合 Tool Call 及其 Result。

Tool Call/Result 按 **toolCallId 一一配对**：孤儿 call 或孤儿 result 均判
违规（数量恰好相等也骗不过检查）；压缩以 call/result 对为最小单位保留或
整体丢弃并在摘要行说明。

CompactionRecord 真实写入：summaryVersion、sourceMessageStartId/EndId、
firstKeptMessageId、before/afterTokenEstimate、reason，并落库
`context_snapshot`（经 `/api/internal/agent-runs/context-snapshots`）。

压缩后一致性检查（违规即告警）：目标未丢、最新请求未丢、配对完整。

实现：`workflow/app/runtime/context.py`；契约由
`run_agent_harness.py::scenario_context_compaction` 与
`tests/test_runtime_core.py` 双重固化。

## Copilot 多轮上下文

Copilot 不复用完整评估 Run 的阈值压缩，因为它的 durable source 是 MySQL
`conversation_message`，每个短答也不创建 `agent_run`。当前链路是：

```text
conversation_message 完整历史（MySQL）
  → 最新 context_snapshot 增量摘要（最多1600字符）
  → 摘要边界之后的最近8条消息（每条最多600字符）
  → 当前问题
  → 同一次 Copilot 回答附带新的 conversationSummary
  → Java 将被移出窗口的旧消息范围和摘要写回 context_snapshot
```

摘要只覆盖被移出最近窗口的消息，不重复概括最近8条；保留用户约束、已确认
事实、关键结论和未解决问题。`source_message_start_id/end_id` 标记本次新压缩
范围，`first_kept_message_id` 标记窗口起点，`summary_version` 单调递增。
简历、JD、最终报告仍按字段单独裁剪，不混进聊天摘要。

普通聊天不开放工具。`BACKGROUND_QUERY` 中涉及技术库、框架或 API 最新文档时，
Copilot 从进程级 MCP Registry 获取 Context7 公网 MCP 的实时 `tools/list`：

- `context7.resolve-library-id`
- `context7.query-docs`

模型通过原生 function call 选择工具，运行时经 MCP `tools/call` 执行；最多两轮，
支持先解析 library id 再查文档。Context7 文档只能回答技术/API问题，不能作为
候选人履历证据。配置来自 `config/mcp-servers.json`，schema 不在本地手写。
