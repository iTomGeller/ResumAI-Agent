# Candidate evidence contract

## Claim status

| Status | Meaning |
| --- | --- |
| `supported` | 可定位证据直接支持完整主张，且没有未解决的实质冲突。 |
| `partially_supported` | 只支持主张的一部分，或深度、ownership、指标口径不完整。 |
| `needs_clarification` | 现有信息含糊，需要候选人或招聘方补充。 |
| `confirmed_conflict` | 两个可定位来源直接冲突，可由人工复核。 |
| `not_checked` | 需要的检索或外部工具没有成功取得可用来源。 |
| `no_signal` | 在当前输入范围内没有发现岗位相关信号。 |

## Source rules

- 简历和用户补充都是候选人自述，不是独立验证。
- RAG chunk 与其原始文档算同一来源。
- 公网结果必须保留 URL、调用时间、工具状态和身份关联。
- `failed`、`unavailable`、`rate_limited`、`not_called` 都不能转换成反证。
- 推断必须与原始事实分字段保存，不能混入 quote。

## Bias boundary

年龄、性别、照片、婚育、民族、地域、学校或公司名气、空窗本身、跳槽次数本身和写作风格不得成为负面评分依据。
