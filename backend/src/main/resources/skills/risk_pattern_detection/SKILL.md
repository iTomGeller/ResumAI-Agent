---
name: risk_pattern_detection
description: 兼容现有 RiskAgent 的岗位相关一致性检查 Skill。检测明确的时间或主张冲突，并控制偏见代理变量；新流程优先结合 calibrate-evidence-confidence 与 audit-job-relevant-evaluation。
---

# Risk Pattern Detection Compatibility

1. 只报告可引用的事实矛盾、时间重叠、指标口径冲突或岗位关键 claim 缺少证明。
2. 区分 `conflicted`、`not_checked` 和 `needs_clarification`。
3. 跳槽次数、空档期、非全日制、专升本、学校或前雇主名气本身不得作为风险。
4. 不推断年龄、性别、民族、婚育、健康或家庭信息。
5. 当前日期缺失时不判断未来或在职时间线异常。
6. 证据不足用于生成核验问题，不写成造假结论。

输出 `risks`、`conflicts`、`clarifyingQuestions`、`sourceRefs`、`unknowns` 和 `toolHealth`。
