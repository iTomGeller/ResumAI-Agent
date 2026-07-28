---
name: evaluate-candidate-evidence
description: 用统一的岗位相关证据契约评估候选人的技术能力、项目贡献与履历风险。Tech、Project、Risk 和 ResumeOptimize Agent 按各自职责选择对应 mode；需要拆分原子主张、绑定来源、判断深度或生成待核实问题时使用。
version: v1
allowed-tools: calculate_jd_coverage resume_semantic_search knowledge_search locate_evidence check_timeline timeline_validator
---

# Evaluate Candidate Evidence

本 Skill 只定义共用证据方法。具体评估维度、输出 artifact 和是否改写简历由当前 Agent 的 system prompt 决定，不在 Skill 内复制四套流程。

需要字段与状态细节时再读取 `references/evidence-contract.md`。

## 输入

- `mode`：`technical | project | risk | rewrite`，由当前 Agent 职责确定。
- `jdRequirements`：带原文引用的岗位要求；没有 JD 时不得补造。
- `candidateClaims`：从简历、用户补充或已有 artifact 拆出的原子主张。
- `sourceRefs`：页码、行号、chunk provenance 或成功公网工具 URL。
- `toolHealth`：真实调用状态；未调用、超时和空结果必须保留。

## 共用流程

1. 只评估与当前 JD 或用户明确目标相关的主张。
2. 把技术、项目、时间线和量化结果拆为原子 claim，并绑定可定位 `sourceRef`。
3. 区分候选人自述、RAG 定位、用户补充和已成功返回的外部证据；RAG 重复 chunk 不增加独立性。
4. 缺证据写 `not_checked` 或 `needs_clarification`，不得写成能力不存在或造假。
5. 只有直接冲突且可人工复核的证据可写 `confirmed_conflict`；工具失败只进入 `toolHealth`。
6. 输出最小、可验证的面试追问，不用公司/学校名气和受保护属性做代理变量。

## Mode 分工

- `technical`：按 JD requirement 评估 `mentioned | used | designed | operated | troubleshot`，不从“使用过”推导“精通”。
- `project`：拆分问题、行动、技术决策、个人 ownership 与结果；检查指标基线、单位、时间窗和测量方式。
- `risk`：先使用确定性时间线结果，再核对语义冲突；空窗或跳槽次数本身不是负面风险。
- `rewrite`：只重排已确认事实；未知数据用 `[待确认]` 或问题表示，不创造人数、QPS、提升比例和主导角色。

## 输出

```json
{
  "mode": "technical",
  "claims": [{
    "claimId": "claim-1",
    "status": "partially_supported",
    "jobRelevance": "对应 JD requirement jd-2",
    "depth": "used",
    "sourceRefs": ["resume:p2:l8"],
    "reason": "有使用证据但缺少设计或排障证据",
    "interviewProbe": "请说明该方案中你独立负责的决策和故障处理。"
  }],
  "confirmedConflicts": [],
  "unknowns": [],
  "toolHealth": {}
}
```

## 边界

- 公网候选人证据由 `retrieve-public-candidate-evidence` 单独处理；本 Skill 不主动联网。
- 外部资料只有工具成功、带来源且身份关联明确时才可支持候选人主张。
- 任何降低推荐等级的结论必须同时给出 source ref、岗位相关性和人工复核理由。
- 评估方法不是候选人事实，不写入 semantic candidate memory。
