# Sandbox Replay Benchmark Report

- Benchmark ID: `bench-20260719-142845-2a636a72`
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
- 策略学习描述为：Policy Optimization Lab（无 GPU）— 生产 champion-only；shadow/lab bandit；有界配置进化（非完整 GEPA）；MODEL_WEIGHTS unchanged。
