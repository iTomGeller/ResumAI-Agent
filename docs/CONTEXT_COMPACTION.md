# Context 压缩

预算：system/policy/skill/recentMessage/memory/toolResult/reservedOutput。

达到模型窗口 70%–80% 触发压缩。保留：最新用户请求、当前目标、取消/限制、
未完成任务、未闭合 Tool Call+Result。老消息生成 Conversation Summary。

一致性检查：目标/修改/取消未丢；Tool Call 与 Result 不被拆开；副作用不重复执行。

实现：`workflow/app/runtime/context.py`
