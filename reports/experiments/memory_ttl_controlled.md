# EXP-14 当前 Workflow Memory TTL 控制实验

- Workflow producer：`861ca1e`
- 全真 run：8（真实模型、完整 Agent/Skill/MCP catalog）
- 数据变更：NONE
- 结论口径：仅声称为当前候选网格、当前 workflow 和明确消费边界下的最优值。

| 类型 | 候选 | 选择 | 当前流程实测 retained-expired Reward |
|---|---|---:|---:|
| WORKING | [1, 2, 3, 7] | 1d | 控制面约束 |
| SEMANTIC | [30, 60, 90, 180] | 90d | -0.0029 |
| EPISODIC | [30, 60, 90, 180] | 90d | 0.0202 |
| PROCEDURAL | [90, 180, 365, 730] | 365d | 0.0042 |

## 边界

时间移位只隔离 TTL 是否让同一条当前版本记忆可见，不会伪装成真实 90/365 天生产历史。
较长 TTL 若与较短 TTL 都覆盖全部边界且质量相同，由最小暴露/存储原则选择较短值；
旧版本、冲突和已归档记忆由版本与生命周期门禁处理，不靠 TTL 掩盖。
