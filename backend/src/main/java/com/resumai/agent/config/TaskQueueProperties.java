package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "resumai.task-queue")
public class TaskQueueProperties {

    private String streamKey = "resumai:task_queue";
    private String consumerGroup = "resume-workers";
    private String workerId = "worker-1";
    /** EXP-QUEUE-20260730 knee on the 4-vCPU ECS: 200 tasks drained in 6.08s. */
    private int maxWorkers = 4;
    private long pollIntervalMs = 100L;
    private int runningTimeoutMinutes = 30;
    private int maxAttempts = 3;
    private int retryBackoffSeconds = 60;
    private boolean enabled = true;

    public String getStreamKey() {
        return streamKey;
    }

    public void setStreamKey(String streamKey) {
        this.streamKey = streamKey;
    }

    public String getConsumerGroup() {
        return consumerGroup;
    }

    public void setConsumerGroup(String consumerGroup) {
        this.consumerGroup = consumerGroup;
    }

    public String getWorkerId() {
        return workerId;
    }

    public void setWorkerId(String workerId) {
        this.workerId = workerId;
    }

    public int getMaxWorkers() {
        return maxWorkers;
    }

    public void setMaxWorkers(int maxWorkers) {
        this.maxWorkers = maxWorkers;
    }

    public long getPollIntervalMs() {
        return pollIntervalMs;
    }

    public void setPollIntervalMs(long pollIntervalMs) {
        this.pollIntervalMs = pollIntervalMs;
    }

    public int getRunningTimeoutMinutes() {
        return runningTimeoutMinutes;
    }

    public void setRunningTimeoutMinutes(int runningTimeoutMinutes) {
        this.runningTimeoutMinutes = runningTimeoutMinutes;
    }

    public int getMaxAttempts() {
        return maxAttempts;
    }

    public void setMaxAttempts(int maxAttempts) {
        this.maxAttempts = maxAttempts;
    }

    public int getRetryBackoffSeconds() {
        return retryBackoffSeconds;
    }

    public void setRetryBackoffSeconds(int retryBackoffSeconds) {
        this.retryBackoffSeconds = retryBackoffSeconds;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }
}
