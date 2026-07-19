# Sandbox Replay Benchmark Report

- Benchmark ID: `bench-20260719-105438-62d14dc0`
- Champion Policy: **strict_evidence**

## Policy Summary

| Policy | Reward | Evidence | Unsupported | JD Coverage | P95 Latency | Fail Rate |
|---|---:|---:|---:|---:|---:|---:|
| balanced | 0.263 | 0.4297 | 0.5703 | 0.3246 | 0ms | 0.0 |
| strict_evidence ← champion | 0.353 | 0.4297 | 0.5703 | 0.3246 | 0ms | 0.0 |
| deep_analysis | 0.138 | 0.4297 | 0.5703 | 0.3246 | 0ms | 0.0 |
| low_cost | 0.3473 | 0.0 | 1.0 | 0.3246 | 0ms | 0.0 |
| backend_job | 0.263 | 0.4297 | 0.5703 | 0.3246 | 0ms | 0.0 |
| agent_job | 0.253 | 0.4297 | 0.5703 | 0.3246 | 0ms | 0.0 |
| resume_rewrite | 0.348 | 0.4297 | 0.5703 | 0.3246 | 0ms | 0.0 |

## Notes

- 所有指标来自真实 Sandbox 工具回放与 `evaluate_policy_output` 评估器。
- Expected Answer 从未注入 Agent/Tool Context。
- 策略学习描述为：基于反馈的 Agent 外层策略学习（epsilon-greedy），非 PPO/GRPO。
