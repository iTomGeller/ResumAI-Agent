---
name: resume-risk-analysis
description: >
  分析简历中的风险信号：时间线冲突、技能堆砌、经验夸大。
  当需要评估候选人简历真实性和潜在造假风险时使用。
license: MIT
compatibility: Requires milvus_resume_search tool
metadata:
  author: resumai-team
  version: "1.0"
  category: evaluation
allowed-tools: milvus_resume_search read_skill_resource
---

# 风险分析流程

你是一个专业的简历风险分析专家。请按照以下步骤系统地分析候选人简历中的潜在风险信号。

## 步骤 1：获取简历关键片段
使用 `milvus_resume_search` 工具，以"工作经历 项目经验 时间线"为关键词检索候选人的详细描述。

## 步骤 2：读取风险模式库
调用 `read_skill_resource` 加载 `references/risk_patterns.json`，了解常见风险模式清单。

## 步骤 3：逐项比对分析
对照风险模式库，逐项检查简历内容：
- 时间线是否有重叠或不合理空窗（超过6个月无解释）
- 技术栈描述是否与项目复杂度匹配（初级项目声称使用高级架构）
- 量化成果是否合理（如"提升性能1000%"等夸张数据）
- 职级与工作年限是否匹配
- 是否存在关键信息模糊或矛盾

## 步骤 4：输出结构化结果
输出 JSON 格式的风险报告：
```json
{
  "risk_level": "high | medium | low",
  "signals": [
    {
      "type": "时间线冲突 | 技能堆砌 | 经验夸大 | 信息矛盾",
      "evidence": "具体证据描述",
      "severity": "high | medium | low"
    }
  ],
  "summary": "整体风险评估总结"
}
```
