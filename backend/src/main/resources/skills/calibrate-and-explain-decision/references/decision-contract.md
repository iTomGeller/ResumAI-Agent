# Decision calibration and explanation contract

## Evidence status

| Status | Required evidence |
| --- | --- |
| `supported` | 直接、可定位的支持证据，无未解决实质冲突。 |
| `partially_supported` | 只支持主张的一部分或证据粒度不足。 |
| `unsupported` | 已检查的可靠来源与主张不符，必须保存反证引用。 |
| `conflicted` | 可靠来源之间存在未解决冲突。 |
| `not_checked` | 没有成功取得可用于检查的来源。 |

## Explanation checklist

每个影响评分或建议的解释至少包含：

1. 被解释的 claim 或评分维度。
2. 原始 `sourceRefs`。
3. 事实、推断与未知的分界。
4. 缺失证据及最小补证问题。
5. 是否需要新 revision；若需要，仅返回受影响 artifact，不静默改写旧报告。

## Interview probe checklist

问题必须可验证一个具体缺口，并提供：

- `triggeredBy` claim；
- `objective`；
- `goodSignals`；
- `redFlags`；
- 可选 `followUps`。
