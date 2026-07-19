---
name: assess-ats-compatibility
description: 检查简历是否可稳定解析，以及相对指定 JD 的精确和语义覆盖度。用户询问 ATS、机器筛选、关键词、投递前检查或按 JD 优化简历时使用。
---

# Assess ATS Compatibility

## 输入

接收 `resumeText`、可选 `resumeStructure`、`formatInspection` 和 `normalizedJd`。

## 流程

1. 先说明可用输入；只有文本时将文件布局标为 `not_checked`。
2. 检查必要章节、时间、联系方式和项目/经历是否被解析到。
3. 对 JD requirement 分别计算精确文本命中、可解释语义命中和无证据。
4. 为每个命中提供 resume source ref；技能清单命中与项目证据命中分开。
5. 给出按影响和改动成本排序的修改建议，避免关键词堆砌。

## 输出

```json
{
  "parseability": {"status": "partial", "issues": [], "notChecked": ["visual_layout"]},
  "coverage": [{"requirementId": "jd-1", "status": "semantic_match", "sourceRefs": ["resume:p1:l8"]}],
  "coverageScore": 68,
  "scoreDefinition": "可解释的 JD 证据覆盖度，不是 ATS 通过概率",
  "prioritizedEdits": [],
  "unknowns": []
}
```

## 证据边界

- 不声称模拟或预测任何厂商 ATS，也不输出“必过”结论。
- 简历未写不等于候选人不会；标为 `no_resume_evidence`。
- 语义命中必须解释对应关系，不把相邻概念当同一技能。
- 不建议隐藏文本、无意义重复或伪造经历。
