package com.resumai.agent.api.dto;

import java.util.List;
import java.util.Map;

/** 候选人历史回填报告。 */
public record CandidateBackfillReport(
        boolean dryRun,
        int batchSize,
        int scanned,
        int wouldLink,
        int linked,
        int reusedProfile,
        int createdProfile,
        int reusedApplication,
        int createdApplication,
        int lowConfidence,
        int nonHrOrigin,
        int skippedAlreadyLinked,
        int failed,
        List<Map<String, Object>> conflictsTopN,
        List<Map<String, Object>> samples
) {
}
