# 多 Agent 协作（Shared State）

## Shared State 字段
resumeFacts / jdRequirements / technicalFindings / projectFindings / risks /
evidence / conflicts / recommendations / agentOutputs / completedTasks /
pendingTasks / artifacts

## Agent 输出契约
agentId, type, claims, evidence, confidence, source, dependencies,
requestedNextAction, createdAt

## 协作原则
1. Agent 只读所需 Shared State（各自的 section 视图）
2. 结论必须附证据（简历/JD/Tool/Memory）
3. 冲突写入 conflicts，不静默覆盖（并行合并同样遵守）
4. EvidenceAgent 只核验 Agent 实际产生的 claims；ReportAgent 基于证据裁决
5. 无法裁决时标记不确定

## 并行执行
Tech/Project/Risk 读取不相交黑板区，Coordinator 按依赖表将其分入同一并行组，
组内 asyncio.gather 并发执行（各自只读视图），组后串行合并输出；
requestedNextAction 委派受 LoopGuard 环检测约束。

实现：`workflow/app/runtime/state.py`、`coordinator.py`、`executor.py`
