package com.resumai.agent.api.hr.dto;

import com.resumai.agent.api.dto.CandidateDomainStats;
import java.util.List;

/** HR 总览 KPI（仅 USER_UPLOAD cohort）。 */
public record HrDashboardResponse(
        long uniqueCandidates,
        long newThisWeek,
        long pendingScreening,
        long pendingReview,
        long interviewStage,
        long offersPending,
        CandidateDomainStats dataCoverage,
        List<HrNextAction> nextActions
) {
}
