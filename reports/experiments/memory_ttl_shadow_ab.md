# EXP-13 当前 Workflow Memory TTL Shadow A/B

- Workflow 切点：`2026-07-29T08:03:16Z`
- cohort：Memory 的生产与消费都来自当前版本。
- 方法：对同一类 gold 简历跑完整 workflow，分别保留或临时过期最新的 ACTIVE `EPISODIC/cross_candidate_anchor`。Gold 标签仅在外部评估器，没有进入 Agent prompt。
- 安全：仅修改当前基准 run 生成的 anchor，实验后恢复 `expires_at/update_time`；复核意外过期数为 0。

| 用例 | 保留 run | 过期 run | 当前 Memory 命中 | Reward Δ | 证据支持率 Δ | 耗时 Δ |
|---|---|---|---:|---:|---:|---:|
| Java 后端正常匹配 | `run-3376b860-8e42-474d-99f6-f3a304a7a462` | `run-0d8168e6-7db3-4db6-a7b8-2f58b7e8fe7d` | 5 vs 0 | +0.0129 | +0.110 | +1.02s |
| AI Agent 强简历 | `run-b836c6aa-0b4b-4303-bc8c-53da0be98bb7` | `run-70179470-3ed4-45a3-a347-d5efb7c0e567` | 5 vs 0 | +0.0251 | +0.148 | -11.14s |

有效 4 个 run 全部 `SUCCEEDED`。保留 Episodic 的平均 Reward 增量为 **+0.019**，平均证据支持率增量为 **+0.129**，平均耗时反而少 **5.06s**；must-find 和违规率均无回退。

结论：当前版本数据支持“Episodic Memory 有正向边际价值，不应过早删除”，因此保留 90 天 incumbent。但这批 anchor 仅有分钟级年龄，**不能证明 90 天优于 30/60 天**；Working/Semantic/Procedural 仍没有当前版本的有效生产→消费对照。
