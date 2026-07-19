---
name: plan-evaluation-revision
description: 计算简历评估输入变化后的新 revision、可复用节点和最小重跑范围。用户修改简历、JD、目标岗位、偏好、评估重点、rubric 或新增外部证据，以及暂停后恢复时使用。
---

# Plan Evaluation Revision

把已发布结果视为不可变 revision。输入变化时创建新 revision，并根据声明式节点依赖计算失效范围。

## 输入

- `baseRevision`：被修改的 revision 标识。
- `artifactHashes`：修改前后的 `resume`、`jd`、`target_role`、`preferences`、`evaluation_focus`、`external_evidence`、`rubric` hash。
- `changedArtifacts`：已由上游 diff 确认的变化类型。
- `completedNodes`：checkpoint 中成功完成的节点。
- `workflowStatus`：当前运行状态。

若只给出 hash，则以 hash 不同生成 `changedArtifacts`。不要让 LLM凭语义相似度决定缓存复用。

## 执行

1. 校验 `baseRevision` 和变化类型。
2. 运行 `scripts/plan_revision.py` 计算确定性计划；需要格式说明时读取 `references/dependency-contract.md`。
3. 将直接依赖变化输入的节点及其所有下游节点标为失效。
4. 仅复用已成功完成、输入 hash 相同且未失效的节点。
5. 选择拓扑顺序中最早的失效节点作为 `restartFrom`。
6. 新 revision 通过 `supersedesRevision` 指向旧 revision，不覆盖旧报告。

## 默认变化类型

- `resume`：从 `intent` 开始重跑。
- `jd` 或 `target_role`：从 `jd_match` 开始重跑。
- `preferences` 或 `evaluation_focus`：从 `intent` 开始重跑。
- `external_evidence`：从 `tech_eval`、`project_eval`、`risk_eval` 开始重跑。
- `rubric`：从三个评估节点开始重跑。
- `conversation_only`：不失效任何评估节点。

调用方提供更准确的运行时依赖图时，以运行时依赖图为准。

## 输出

```json
{
  "baseRevision": "rev-3",
  "newRevision": "rev-4",
  "changedArtifacts": ["jd"],
  "invalidateNodes": ["jd_match", "knowledge_context", "tech_eval", "project_eval", "risk_eval", "evidence_fusion", "report"],
  "reuseNodes": ["intent", "resume_parse"],
  "restartFrom": "jd_match",
  "supersedesRevision": "rev-3",
  "reasonByNode": {"jd_match": "direct input changed", "report": "downstream of changed input"},
  "needsConfirmation": false
}
```

## 边界

- 不根据旧输出“看起来还能用”而复用节点。
- 不修改、删除或重新标记旧 revision。
- 取消与暂停属于运行控制，不自行创建内容 revision；只有输入变化才创建。
- 未知变化类型返回 `needsConfirmation=true`，不要默认全量或零重跑。
- 此 Skill 只规划状态，不生成候选人事实或评分。
