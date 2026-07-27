---
name: risk_pattern_detection
description: 基于目标岗位和可定位证据检查简历中的时间线冲突、主张不一致、职责边界与待核验风险。RiskAgent 需要风险判断、用户要求核验履历，或报告包含负面结论时使用；不得把未知或未联网核验写成造假。
version: v2
allowed-tools: check_timeline knowledge_search
---

# Risk Pattern Detection

只报告与目标岗位和招聘决策直接相关、且能定位到证据的风险。风险识别不是人格推断，也不是背景偏见评分。

## 输入与分层

1. 将简历拆成带 source ref 的时间、角色、职责、项目和量化结果主张。
2. 先运行确定性的时间线检查，再对语义冲突做证据核对。
3. 将结论分为：
   - `confirmed_conflict`：两个可定位来源直接冲突；
   - `needs_clarification`：信息不足或描述含糊，需要面试确认；
   - `not_checked`：依赖外部证据，但工具未调用、失败或无身份绑定；
   - `no_signal`：未发现岗位相关风险信号。
4. 只有 `confirmed_conflict` 可以影响履历可信度分；其余状态只能生成追问。

## 允许检查的信号

- 任职或教育时间重叠且简历没有合理说明。
- 同一项目的角色、技术栈或指标在不同位置直接矛盾。
- 量化结果缺少基线、单位、时间窗或个人贡献边界。
- 技术发布时间与候选人声明使用时间明显不可能，并有真实文档证据。

## 禁止的代理变量

不得因年龄、性别、照片、婚育、民族、地域、学校名气、公司名气、空窗本身、跳槽次数本身或写作风格给负面风险。空窗和频繁变动只有在 JD 存在明确、合法且岗位相关的稳定性要求并经人工确认时，才可作为待澄清问题。

## 输出

```json
{
  "signals": [{
    "claimId": "",
    "status": "needs_clarification",
    "severity": "low",
    "reason": "",
    "sourceRefs": [],
    "jobRelevance": "",
    "interviewProbe": ""
  }],
  "confirmedConflictCount": 0,
  "notChecked": [],
  "biasAudit": {"protectedOrProxyFeaturesUsed": []}
}
```

## 失败边界

- RAG 无命中不证明主张为假。
- 公网 MCP 超时、限流、鉴权缺失或空结果一律记 `not_checked`。
- 不把公司技术博客中的团队成果自动归于候选人个人。
- 任何会降低推荐等级的风险必须同时给出 source ref、岗位相关性和可由人工复核的理由。
