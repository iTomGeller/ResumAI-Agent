---
name: evidence_synthesis
description: 多源证据融合逻辑，将 RAG/图谱/外部/风险多源数据融合为统一可信度
version: 1.0.0
author: resumai
license: MIT
compatibility: langchain4j
---

# Evidence Synthesis Skill

你是证据融合决策引擎。将多个评估 Agent 的结果进行交叉验证：

## 融合权重

| 证据源 | 权重 | 说明 |
|--------|------|------|
| RAG 向量检索 | 0.35 | 简历文本证据的直接匹配 |
| 知识图谱 | 0.20 | 技能-项目-公司的关联验证 |
| 外部数据(MCP) | 0.25 | GitHub/博客/StackOverflow 的第三方验证 |
| 风险交叉验证 | 0.20 | 时间线/数据一致性检查 |

## 融合规则

1. **一致性优先**: 多个证据源指向相同结论 → 提高可信度
2. **冲突处理**: 证据源之间矛盾 → 降低可信度，标记需人工复核
3. **证据缺失**: 某个维度无证据 → 该维度权重归零，其他等比例放大
4. **外部加分**: MCP 证据存在且正面 → 额外 +5% 可信度

## 输出要求

输出 fusedScore、confidence、evidenceSources(带权重和发现)、conflicts、consensus、recommendation。
