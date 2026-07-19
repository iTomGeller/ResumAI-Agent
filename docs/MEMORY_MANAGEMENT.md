# 分层 Memory

类型：Working / Conversation / Episodic / UserPreference / HR Feedback / Domain / Failure

每条 Memory：memoryId, type, ownerScope, userId, conversationId, runId,
content, structuredContent, source, confidence, status, expiresAt, version,
embedding, sensitivityLevel

规则：Scope 隔离、相关性排序、时间衰减、去重、冲突处理、敏感信息过滤。
ContextManager 只加载当前任务相关 Memory（`workflow/app/runtime/memory.py`）。
Secret 永不进入 Memory/Prompt/Trace 明文。
