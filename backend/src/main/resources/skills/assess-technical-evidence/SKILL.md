---
name: assess-technical-evidence
description: 根据具体 JD 和候选人可定位证据评估技术主张、深度与缺口。需要技术栈评估、岗位相关评分、技术证据核验或生成技术追问时使用。
allowed-tools: context7.resolve-library-id context7.query-docs microsoft-learn.microsoft_docs_search microsoft-learn.microsoft_docs_fetch microsoft-learn.microsoft_code_sample_search
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

## 文档工具边界

- 仅当简历出现明确框架/平台，且当前官方行为会影响一个 JD 关键主张时，才使用本轮实际暴露的文档 MCP；不要为凑调用覆盖率而检索。
- Context7 适用于非 Microsoft 框架，先解析准确 library ID，再做一个聚焦主题查询；Microsoft 技术栈使用 Microsoft Learn 的搜索、文档或代码样例工具。
- 文档结果只能说明框架/API 的当前能力，不能证明候选人真的做过；候选人事实仍必须绑定简历、项目或已核验外链。
- 已有证据足够完成岗位相关判断时可以不调用文档工具，并在 `toolHealth` 中保持 `not_called`。

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
