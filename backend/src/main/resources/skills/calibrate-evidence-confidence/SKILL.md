---
name: calibrate-evidence-confidence
description: 对简历原文、RAG、JD、用户补充和真实外部工具结果进行逐主张证据校准。需要融合来源、去重、处理冲突、区分未检查与不支持，或为最终报告生成可信度说明时使用。
---

# Calibrate Evidence Confidence

以 claim 为单位融合已经完成来源审计的证据，不使用固定来源权重。输入记录含义不清时可读取 `references/evidence-record.md`；该参考只解释证据记录，不定义 Agent 最终输出。

## 输入

接收 evidence record 列表。每条至少包含：

- `claimId`、`claimText`。
- `sourceType`：`resume_text | user_statement | rag_chunk | jd_text | external_tool`。
- `sourceRef`、`quote`、`retrievedAt`。
- `toolStatus`：`success | unavailable | failed | not_called`。
- `identityLinkage`：外部账号与候选人的关联状态。

## 工作流

1. 按 `claimId` 分组。
2. 消费来源审计给出的重复、派生、工具状态和身份绑定关系。
3. 对同一主张判断支持范围，不把部分支持扩大为完整支持。
4. 记录直接冲突、时间冲突和口径差异，不擅自选择有利版本。
5. 根据证据状态形成结论，不因来源数量机械加分。

## 状态定义

- `supported`：存在可定位的直接证据，且无未解决实质冲突。
- `partially_supported`：只支持 claim 的一部分或证据粒度不足。
- `unsupported`：已检查的可靠来源与 claim 不符；必须给出反证。
- `conflicted`：可靠来源之间存在未解决冲突。
- `not_checked`：没有成功取得可用于检查的来源。

`not_checked` 绝不能降级为 `unsupported` 或风险结论。

## 交付

通过当前 Agent 的统一输出契约提交逐主张状态、置信区间、冲突、未知项和证据缺口。不要定义、复述或包裹另一套 JSON Schema。

## 边界

- 工具失败只写入 `toolHealth` 和 `unknowns`。
- 不引用未启用的 GitHub、博客、StackOverflow、图谱或数据库。
- 不因外部结果“正面”额外加分。
- RAG 是定位手段，不是独立事实来源；保留其原始文档 provenance。
- 缺少证明不等于候选人撒谎，也不等于候选人没有该能力。
