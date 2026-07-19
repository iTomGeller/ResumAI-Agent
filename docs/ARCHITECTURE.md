# ResumAI 对话式简历评估 Agent 架构

```text
Frontend (Conversation SSE)
    → Spring Boot (Conversation/Run/Policy/Memory APIs, Redis Queue+Lock)
        → Python Workflow Agent Runtime (Coordinator/Agents/Tools/MCP)
            → Sandbox Manager → Ephemeral Docker Worker (network=none)
```

关联 ID：userId, conversationId, runId, traceId

核心模块：AgentRegistry, Coordinator, Runtime/Executor, RunScheduler,
SharedState, Prompt/Skill/Memory/Context Managers, Tool/MCP Registry,
SandboxClient, TrajectoryRecorder, PolicySelector, RewardCalculator

业务 Agent：ResumeParser, JDAnalysis, Tech, Project, Risk, Evidence,
Report, ResumeOptimize, InterviewQuestion, Coordinator
