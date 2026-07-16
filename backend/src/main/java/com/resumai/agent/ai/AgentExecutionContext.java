package com.resumai.agent.ai;

/**
 * ThreadLocal context that identifies which trace/agent the current thread is executing.
 * Set by ResumeEvaluationOrchestrator.runAgent(), read by TracingChatModelListener.
 */
public final class AgentExecutionContext {

    private static final ThreadLocal<Context> CURRENT = new ThreadLocal<>();

    public static void set(String traceId, String agentName) {
        CURRENT.set(new Context(traceId, agentName));
    }

    public static Context get() {
        return CURRENT.get();
    }

    public static void clear() {
        CURRENT.remove();
    }

    public record Context(String traceId, String agentName) {}
}
