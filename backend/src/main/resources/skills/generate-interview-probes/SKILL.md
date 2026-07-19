---
name: generate-interview-probes
description: 围绕具体项目主张、JD 能力项、证据缺口和冲突生成递进面试追问。需要报告中的面试问题、模拟面试或核验某项经历时使用。
---

# Generate Interview Probes

## 输入

接收 `claims`、`evidenceGaps`、`normalizedJd`、`experienceLevel` 和期望的问题数量。

## 流程

1. 优先选择岗位关键且证据不足、冲突或影响评分的 claim。
2. 每组生成一个开放主问题和最多三个由浅入深的 follow-up。
3. 写明验证目标、可接受证据、强信号和弱信号。
4. 将问题绑定到 `claimId`、`requirementId` 和 source ref。

## 输出

```json
{
  "probes": [{
    "question": "请画出该项目请求链路，并说明你负责的边界。",
    "followUps": [],
    "claimId": "p1-c2",
    "requirementId": "jd-3",
    "verificationGoal": "区分团队方案与个人贡献",
    "strongSignals": [],
    "weakSignals": []
  }]
}
```

## 证据边界

- 以验证为目的，不在问题中预设造假或有罪。
- 候选人的解释是新证据输入，不自动判真或判假。
- 不询问受保护属性、家庭或与岗位无关的个人信息。
- 不生成脱离简历和 JD 的通用题库填充数量。
