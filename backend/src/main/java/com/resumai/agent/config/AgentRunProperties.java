package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConfigurationProperties(prefix = "resumai.agent-run")
public class AgentRunProperties {

    /** Master switch for the conversational run scheduler. */
    private boolean enabled = true;

    /** Global maximum number of concurrently RUNNING agent runs. */
    private int maxGlobalConcurrent = 4;

    /** Default wall-clock budget for one run (seconds). */
    private int runTimeoutSeconds = 900;

    /** Dispatcher poll interval (ms). */
    private long dispatchIntervalMs = 500;

    /** Watchdog scan interval (ms). */
    private long watchdogIntervalMs = 15000;

    /** How long a CANCELLING run may linger before it is force-closed (s). */
    private int cancelGraceSeconds = 90;

    /** Lease TTL for conversation/global permits (minutes). */
    private int permitLeaseMinutes = 30;

    /** Worker identity for diagnostics. */
    private String workerId = "backend-1";

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public int getMaxGlobalConcurrent() {
        return maxGlobalConcurrent;
    }

    public void setMaxGlobalConcurrent(int maxGlobalConcurrent) {
        this.maxGlobalConcurrent = maxGlobalConcurrent;
    }

    public int getRunTimeoutSeconds() {
        return runTimeoutSeconds;
    }

    public void setRunTimeoutSeconds(int runTimeoutSeconds) {
        this.runTimeoutSeconds = runTimeoutSeconds;
    }

    public long getDispatchIntervalMs() {
        return dispatchIntervalMs;
    }

    public void setDispatchIntervalMs(long dispatchIntervalMs) {
        this.dispatchIntervalMs = dispatchIntervalMs;
    }

    public long getWatchdogIntervalMs() {
        return watchdogIntervalMs;
    }

    public void setWatchdogIntervalMs(long watchdogIntervalMs) {
        this.watchdogIntervalMs = watchdogIntervalMs;
    }

    public int getCancelGraceSeconds() {
        return cancelGraceSeconds;
    }

    public void setCancelGraceSeconds(int cancelGraceSeconds) {
        this.cancelGraceSeconds = cancelGraceSeconds;
    }

    public int getPermitLeaseMinutes() {
        return permitLeaseMinutes;
    }

    public void setPermitLeaseMinutes(int permitLeaseMinutes) {
        this.permitLeaseMinutes = permitLeaseMinutes;
    }

    public String getWorkerId() {
        return workerId;
    }

    public void setWorkerId(String workerId) {
        this.workerId = workerId;
    }
}
