package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConfigurationProperties(prefix = "resumai.agent-run")
public class AgentRunProperties {

    /** Master switch for the conversational run scheduler. */
    private boolean enabled = true;

    /** Global maximum number of concurrently RUNNING agent runs. */
    private int maxGlobalConcurrent = 12;

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

    /** How long a PAUSED run may keep its conversation reserved (seconds). */
    private int pauseTtlSeconds = 7200;

    /** How long PAUSING may wait for the snapshot callback before reverting (s). */
    private int pauseGraceSeconds = 120;

    /**
     * After restart, STARTING / RUNNING without a live runtime may be
     * re-dispatched or resumed within this grace window; past grace with no
     * runtime and no checkpoint becomes ORPHANED_ON_RESTART.
     */
    private int startGraceSeconds = 90;

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

    public int getPauseTtlSeconds() {
        return pauseTtlSeconds;
    }

    public void setPauseTtlSeconds(int pauseTtlSeconds) {
        this.pauseTtlSeconds = pauseTtlSeconds;
    }

    public int getPauseGraceSeconds() {
        return pauseGraceSeconds;
    }

    public void setPauseGraceSeconds(int pauseGraceSeconds) {
        this.pauseGraceSeconds = pauseGraceSeconds;
    }

    public int getStartGraceSeconds() {
        return startGraceSeconds;
    }

    public void setStartGraceSeconds(int startGraceSeconds) {
        this.startGraceSeconds = startGraceSeconds;
    }

    public String getWorkerId() {
        return workerId;
    }

    public void setWorkerId(String workerId) {
        this.workerId = workerId;
    }
}
