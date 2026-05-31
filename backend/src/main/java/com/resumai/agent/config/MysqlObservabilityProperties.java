package com.resumai.agent.config;

import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "resumai.mysql.observability")
public class MysqlObservabilityProperties {

    /** 是否启用 MyBatis SQL 与表容量观测。 */
    private boolean enabled = true;

    /** 慢查询阈值（毫秒），超过则计入慢查询计数。 */
    private long slowQueryThresholdMs = 200L;

    /** 表容量指标刷新间隔（毫秒）。 */
    private long tableRefreshIntervalMs = 60_000L;

    /** 需要采集行数与容量的核心表。 */
    private List<String> monitoredTables = List.of(
            "jd_library",
            "resume_task",
            "agent_execution_trace",
            "llm_invocation",
            "human_feedback_log",
            "ragas_eval_metrics",
            "system_orchestration_rule");

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public long getSlowQueryThresholdMs() {
        return slowQueryThresholdMs;
    }

    public void setSlowQueryThresholdMs(long slowQueryThresholdMs) {
        this.slowQueryThresholdMs = slowQueryThresholdMs;
    }

    public long getTableRefreshIntervalMs() {
        return tableRefreshIntervalMs;
    }

    public void setTableRefreshIntervalMs(long tableRefreshIntervalMs) {
        this.tableRefreshIntervalMs = tableRefreshIntervalMs;
    }

    public List<String> getMonitoredTables() {
        return monitoredTables;
    }

    public void setMonitoredTables(List<String> monitoredTables) {
        this.monitoredTables = monitoredTables;
    }
}
