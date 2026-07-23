package com.resumai.agent.api.dto;

/** 候选人域数据完整性统计。 */
public record CandidateDomainStats(
        long activeTasks,
        long linkedTasks,
        long unlinkedTasks,
        long skippedTasks,
        long verifiedProfiles,
        long legacyUnverifiedProfiles,
        double linkRatio,
        /** LEGACY_TASKS / DUAL / CANDIDATE_ONLY */
        String mode
) {
}
