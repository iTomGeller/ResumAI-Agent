# Real Agent E2E Quality Benchmark

- Benchmark ID: `e2e-20260719-170639-b0e9a87d`
- Champion Policy: **low_cost**（真实 E2E 平均 Reward 最高）
- 模型: deepseek-chat  重复次数/用例: 3
- 每一行都对应一次真实 /agent/runs 执行：真实 Coordinator、真实 DeepSeek、
  真实 Sandbox Docker Worker；LLM 次数与 Token 来自 runtime metrics，
  成本按 DeepSeek 官方单价由真实 Token 计算。
- mustFind/mustNotClaim/expectedRisk 只进入评估器，从未进入 Agent 输入。

## Policy Summary

| Policy | Reward | Success | LLM Calls | Tokens(P/C) | Cost(CNY) | Avg Latency | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| balanced | 0.3725 | 1.0 | 6.44 | 10734/6809 | 0.075948 | 37.63s | 49.32s |
| strict_evidence | 0.3529 | 1.0 | 6.56 | 10559/5971 | 0.068888 | 35.41s | 50.54s |
| low_cost ← champion | 0.4629 | 1.0 | 3.67 | 4431/2911 | 0.032151 | 22.38s | 33.7s |
