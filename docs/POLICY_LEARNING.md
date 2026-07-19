# Agent 外层策略学习（无 GPU）

**不是** DeepSeek 训练 / RLHF / PPO / GRPO。

PolicyBundle 控制：Agent Team/Order、Prompt/Skill 版本、Tool/Context Budget、
Memory 检索、证据核验、改写轮次、超时策略。

策略：balanced, strict_evidence, deep_analysis, low_cost, backend_job, agent_job, resume_rewrite

选择：epsilon-greedy（默认 ε=0.1）；样本充足时 Thompson Sampling。

反馈 → RewardCalculator → PolicyStatistics → 后续选择变化。

实现：`PolicyService.java`、`RewardService.java`
