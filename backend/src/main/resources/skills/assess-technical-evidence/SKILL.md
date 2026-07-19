---
name: assess-technical-evidence
description: 根据具体 JD 和候选人可定位证据评估技术主张、深度与缺口。需要技术栈评估、岗位相关评分、技术证据核验或生成技术追问时使用。
---

# Assess Technical Evidence

## 输入

接收 `normalizedJd`、`resumeClaims`、`projectClaims`、可选 `externalEvidence` 和 `experienceLevel`。

## 流程

1. 从 JD requirement 建立评估维度；不使用固定的通用技术清单。
2. 将每个技术主张绑定到简历或项目 source ref。
3. 区分 `mentioned | used | designed | operated | troubleshot | externally_supported`。
4. 根据岗位要求判断覆盖与深度，不从“使用过”推导“精通”。
5. 为证据不足但岗位关键的项目生成追问。

## 输出

```json
{
  "dimensions": [{"requirementId": "jd-2", "claim": "", "depth": "used", "status": "partially_supported", "sourceRefs": []}],
  "overallTechScore": 0,
  "scoreBasis": [],
  "strengths": [],
  "gaps": [],
  "interviewChecks": [],
  "toolHealth": {}
}
```

## 证据边界

- AI/ML 只在 JD 相关时进入评分，不作为所有岗位固定加分项。
- 外部资料只有真实工具成功返回且身份关联明确时使用。
- RAG chunk 只用于定位原文，不作为额外独立证明。
- 没有生产证据时标未知，不推断候选人没有能力。
