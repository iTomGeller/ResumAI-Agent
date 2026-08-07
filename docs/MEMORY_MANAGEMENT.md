# 分层 Memory

正式类型只有三种：Semantic / Episodic / Procedural。

- Semantic：候选人稳定事实、用户明确偏好；默认 90 天。
- Episodic：一次已成功评估的证据链、风险与对比锚点；默认 90 天。
- Procedural：经验证、候选人无关的执行策略；默认 365 天。

Working Memory 已从 Runtime 读写链路删除。Run 恢复依赖 `execution_snapshot`，不再把 checkpoint/scratch 数据写进 Memory 表。Python 只把三类长期 Memory 候选放入最终 SharedState，Java 接受成功终态后直接写入；失败、取消或未接受的 Run 不写。

每条 Memory：memoryId, type, ownerScope, userId, conversationId, runId,
content, structuredContent, source, confidence, status, expiresAt, version,
embedding, sensitivityLevel

规则：Scope 隔离、相关性排序、时间衰减、去重、冲突处理、敏感信息过滤。
ContextManager 只加载当前任务相关 Memory（`workflow/app/runtime/memory.py`）。
Secret 永不进入 Memory/Prompt/Trace 明文。
