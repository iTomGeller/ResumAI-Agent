# 多 Agent 协作（Shared State）

## Shared State 字段
resumeFacts / jdRequirements / technicalFindings / projectFindings / risks /
evidence / conflicts / recommendations / agentOutputs / completedTasks /
pendingTasks / artifacts

## Agent 输出契约
agentId, type, claims, evidence, confidence, source, dependencies,
requestedNextAction, createdAt

## 协作原则
1. Agent 只读所需 Shared State
2. 结论必须附证据（简历/JD/Tool/Memory）
3. 冲突写入 conflicts，不静默覆盖
4. EvidenceAgent 核验；ReportAgent 基于证据生成
5. 无法裁决时标记不确定

实现：`workflow/app/runtime/state.py`、`coordinator.py`、`executor.py`
