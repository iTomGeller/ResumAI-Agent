# Policy Contract Benchmark Report（非 Agent 质量基准）

- Benchmark ID: `contract-20260719-162333-3053918c`
- 类型：**Contract Benchmark**（工具契约 / 评分公式 / 安全规则 / 故障注入回归）
- 本报告不运行 Coordinator/RunExecutor/DeepSeek，不创建 Docker Worker，
  数字不代表真实 Agent 质量，**不用于选择 Champion Policy**。
- 真实质量基准见 `run_agent_e2e_benchmark.py` 的输出。

## Per-Policy Contract Results

| Policy | Reward(合约) | Evidence | Unsupported | JD Coverage | P95 Latency | Fail Rate |
|---|---:|---:|---:|---:|---:|---:|
| balanced | 0.3418 | 0.7222 | 0.2778 | 0.3426 | 0ms | 0.0 |
| strict_evidence | 0.4319 | 0.7222 | 0.2778 | 0.3426 | 0ms | 0.0 |
| deep_analysis | 0.2168 | 0.7222 | 0.2778 | 0.3426 | 0ms | 0.0 |
| low_cost | 0.3563 | 0.0 | 1.0 | 0.3426 | 0ms | 0.0 |
| backend_job | 0.3418 | 0.7222 | 0.2778 | 0.3426 | 0ms | 0.0 |
| agent_job | 0.3318 | 0.7222 | 0.2778 | 0.3426 | 0ms | 0.0 |
| resume_rewrite | 0.4269 | 0.7222 | 0.2778 | 0.3426 | 0ms | 0.0 |

## Failure Injection

- 注入用例数: 7，编造被评估器惩罚: 2

## Notes

- 指标来自确定性 Sandbox 工具（与 Docker Worker 同源代码）与 `evaluate_policy_output` 评估器。
- mustFind/mustNotClaim 仅进入评估器；工具核验使用解析产物派生的 claims。
- llmCalls/cost 为名义值（本基准零 LLM 调用），仅用于奖励公式回归。
