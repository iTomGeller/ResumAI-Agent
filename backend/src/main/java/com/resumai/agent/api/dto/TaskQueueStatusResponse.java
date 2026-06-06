package com.resumai.agent.api.dto;

import java.util.List;

public record TaskQueueStatusResponse(
        long queued,
        long running,
        long retrying,
        long failed,
        long stuck,
        long pendingMessages,
        long oldestWaitSeconds,
        int activeWorkers,
        int workerCapacity,
        double workerUtilization,
        List<String> recentFailures
) {
}
