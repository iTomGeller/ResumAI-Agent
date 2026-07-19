# Agent 外层策略学习（无 GPU）

**不是** DeepSeek 训练 / RLHF / PPO / GRPO。

PolicyBundle 控制：Agent Team/Order、并行开关、Prompt/Skill 版本、
Tool/Context Budget、Memory 检索、证据核验、改写轮次、超时策略。

策略：balanced, strict_evidence, deep_analysis, low_cost, backend_job,
agent_job, resume_rewrite

选择：epsilon-greedy（默认 ε=0.1）；每臂样本充足时 Thompson Sampling；
Benchmark/回放可用 `forcedPolicyId` 固定策略（记录 selection_mode=FORCED，
不污染探索统计）。

反馈闭环：HR Feedback → RewardService（分量单独入 policy_reward.components）
→ policy_statistics 更新 → 后续同类任务选择变化。

Champion 选择：只允许来自真实 Agent E2E Benchmark
（`harness/run_agent_e2e_benchmark.py`，真实 LLM/Token/Sandbox 指标）。
Contract Benchmark 只做配置/公式/安全回归，不产生质量结论。

实现：`PolicyService.java`、`RewardService.java`
