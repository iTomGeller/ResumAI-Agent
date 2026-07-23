# Policy Optimization Lab（无 GPU）

**不是** DeepSeek 训练 / RLHF / PPO / GRPO。**不是** 完整 GEPA。

并列能力轴（Ops / Docs 统一口径）：

| 轴 | 含义 |
|---|---|
| `ONLINE_SELECTION` | 生产招聘决策 **champion-only**；epsilon / Thompson bandit 仅 shadow / lab |
| `OFFLINE_SEARCH` | LLM 引导的有界配置进化（`harness/evolve_policies.py`），非完整 GEPA |
| `MODEL_WEIGHTS` | unchanged — 永不训练模型权重 |
| `SANDBOX` | 实验隔离（Policy Lab）；不是候选人评估路径 |

PolicyBundle 控制：Agent Team/Order、并行开关、Prompt/Skill 版本、
Tool/Context Budget、Memory 检索、证据核验、改写轮次、超时策略。

策略：balanced, strict_evidence, deep_analysis, low_cost, backend_job,
agent_job, resume_rewrite

选择：
- 生产 `PRODUCTION_DECISION`：只取当前 champion（无探索）。
- Shadow / Lab：epsilon-greedy（默认 ε=0.1）；每臂样本充足时 Thompson Sampling。
- Benchmark/回放可用 `forcedPolicyId` 固定策略（记录 selection_mode=FORCED）。

反馈闭环：HR Feedback → RewardService（分量单独入 policy_reward.components）
→ policy_statistics 更新 → 仅影响后续 lab/shadow 选择与离线晋升。

Reward 要点：
- `PARTIAL_SUCCESS` ≠ `SUCCEEDED`（部分成功分）。
- 无 evidence 时 `evidenceSupportRatio` 记为未定义 / 0，不得默认成 1。
- 可用时纳入 `timeline_hit` / `unsupportedClaimRate` / `expectedRisk`。

Champion 选择：只允许来自真实 Agent E2E Benchmark
（`harness/run_agent_e2e_benchmark.py`，真实 LLM/Token/Sandbox 指标）。
Contract Benchmark 只做配置/公式/安全回归，不产生质量结论。

实验表（V14）：`policy_experiment` / `policy_candidate` / `policy_trial`
（`policy_evolution_log` 仍作事件日志）。

实现：`PolicyService.java`、`RewardService.java`、`harness/evolve_policies.py`、
`workflow/app/policy_lab/`
