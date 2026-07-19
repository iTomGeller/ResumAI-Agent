# Prompt 管理

Prompt 统一由 `workflow/app/runtime/prompts.py` 的 PromptManager 管理，不散落硬编码。

每条 Prompt：promptId, agentId, version, content, hash, status, metrics。

组装顺序只加载当前任务需要的 System/Task/Policy/Skill/Memory/Tool/Output Schema。
Trace 记录 promptVersions；可与 Policy/Benchmark 绑定并回滚。
