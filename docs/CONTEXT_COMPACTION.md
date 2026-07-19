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
