# 多 Agent 协作（Shared State）

## Shared State 字段
resumeFacts / jdRequirements / technicalFindings / projectFindings / risks /
evidence / conflicts / recommendations / agentOutputs / completedTasks /
pendingTasks / artifacts

## Agent 输出契约
agentId, type, claims, evidence, source, dependencies, createdAt

## 协作原则
1. Agent 只读所需 Shared State（各自的 section 视图）
2. 结论必须附证据（简历/JD/Tool/Memory）
3. 冲突写入 conflicts，不静默覆盖（并行合并同样遵守）
4. ReportAgent 只采纳能由简历、JD、RAG或真实工具回执支撑的结论
5. 无法支撑时写入 missingEvidence、风险或面试追问

## 并行执行
Tech/Project/Risk 读取不相交黑板区，Coordinator 按依赖表将其分入同一并行组，
LangGraph 使用 Send 并发执行（各自只读视图），组后按原计划顺序串行合并输出。
运行期间不接受 Agent handoff，也不修改初始计划。

实现：`workflow/app/runtime/state.py`、`coordinator.py`、`executor.py`
