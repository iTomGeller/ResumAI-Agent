---
name: intent_routing
description: 兼容现有 IntentAgent 的评估策略路由 Skill。根据简历和已确认目标岗位选择评估重点；多轮用户消息、临时岔题和控制请求应使用 route-conversation-turn。
---

# Intent Routing Compatibility

1. 基于简历原文和已确认目标岗位生成 `candidateType`、`experienceLevel`、`targetRole`、`routingHints`、`requiredSkills`、`evidenceGaps` 和 `interviewFocus`。
2. 经验等级不只按工作年限判断；保留 `unknown` 并引用依据。
3. 不使用“技术词占比超过 50%”等机械阈值。
4. 不处理暂停、取消、恢复、目标替换或临时问题；这些交给 `route-conversation-turn`。
5. 路由提示是执行策略，不是候选人事实证据或最终评分。

只输出紧凑 JSON，并为推断提供简短 `sourceRefs`。
