package com.resumai.agent.service.hr;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.resumai.agent.api.dto.CandidateDomainStats;
import com.resumai.agent.api.hr.dto.HrDashboardResponse;
import com.resumai.agent.api.hr.dto.HrNextAction;
import com.resumai.agent.dao.CandidateApplicationMapper;
import com.resumai.agent.dao.CandidateProfileMapper;
import com.resumai.agent.domain.entity.CandidateApplication;
import com.resumai.agent.domain.entity.CandidateProfile;
import com.resumai.agent.domain.enums.DataOrigin;
import com.resumai.agent.service.candidate.CandidateBackfillService;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/** HR 总览：只统计 USER_UPLOAD cohort。 */
@Service
public class HrDashboardService {

    private static final Set<String> REVIEW_RECS = Set.of(
            "NEED_MANUAL_REVIEW", "MANUAL_REVIEW", "HOLD", "BORDERLINE");

    private final CandidateProfileMapper profileMapper;
    private final CandidateApplicationMapper applicationMapper;
    private final CandidateBackfillService backfillService;

    public HrDashboardService(CandidateProfileMapper profileMapper,
                              CandidateApplicationMapper applicationMapper,
                              CandidateBackfillService backfillService) {
        this.profileMapper = profileMapper;
        this.applicationMapper = applicationMapper;
        this.backfillService = backfillService;
    }

    public HrDashboardResponse dashboard() {
        CandidateDomainStats coverage = backfillService.stats();

        long uniqueCandidates = count(profileMapper.selectCount(hrProfiles()));
        LocalDateTime weekAgo = LocalDateTime.now().minusDays(7);
        long newThisWeek = count(profileMapper.selectCount(hrProfiles().ge("create_time", weekAgo)));

        List<CandidateApplication> hrApps = loadHrApplications();
        long pendingScreening = 0;
        long pendingReview = 0;
        long interviewStage = 0;
        long offersPending = 0;
        for (CandidateApplication app : hrApps) {
            String stage = app.getStage() == null ? "" : app.getStage().trim().toUpperCase(Locale.ROOT);
            if ("NEW".equals(stage) || "SCREENING".equals(stage)) {
                pendingScreening++;
            }
            if ("INTERVIEW".equals(stage)) {
                interviewStage++;
            }
            if ("OFFER".equals(stage)) {
                offersPending++;
            }
            String rec = app.getLatestRecommendation() == null
                    ? "" : app.getLatestRecommendation().trim().toUpperCase(Locale.ROOT);
            if (REVIEW_RECS.contains(rec)
                    || (!StringUtils.hasText(rec) && ("SCREENING".equals(stage) || "NEW".equals(stage))
                    && app.getLatestScore() != null && app.getLatestScore() < 70)) {
                pendingReview++;
            }
        }

        List<HrNextAction> actions = new ArrayList<>();
        if (pendingScreening > 0) {
            actions.add(new HrNextAction("SCREENING", "待筛选候选人", "#/candidates?stage=SCREENING", pendingScreening));
        }
        if (pendingReview > 0) {
            actions.add(new HrNextAction("REVIEW", "待人工复核", "#/candidates", pendingReview));
        }
        if (interviewStage > 0) {
            actions.add(new HrNextAction("INTERVIEW", "面试进行中", "#/candidates?stage=INTERVIEW", interviewStage));
        }
        if (offersPending > 0) {
            actions.add(new HrNextAction("OFFER", "Offer 待处理", "#/candidates?stage=OFFER", offersPending));
        }
        if (coverage.unlinkedTasks() > 0) {
            actions.add(new HrNextAction(
                    "MIGRATE",
                    "未迁移评估 " + coverage.unlinkedTasks() + " 条",
                    "#/dev/ops",
                    coverage.unlinkedTasks()));
        }

        return new HrDashboardResponse(
                uniqueCandidates,
                newThisWeek,
                pendingScreening,
                pendingReview,
                interviewStage,
                offersPending,
                coverage,
                actions);
    }

    private QueryWrapper<CandidateProfile> hrProfiles() {
        return new QueryWrapper<CandidateProfile>()
                .and(w -> w.eq("data_origin", DataOrigin.USER_UPLOAD.name())
                        .or().isNull("data_origin"));
    }

    private List<CandidateApplication> loadHrApplications() {
        // 通过 candidate 的 data_origin 过滤；无 join 时先取 HR candidate ids
        List<CandidateProfile> profiles = profileMapper.selectList(
                hrProfiles().select("id"));
        if (profiles.isEmpty()) {
            return List.of();
        }
        List<Long> ids = profiles.stream().map(CandidateProfile::getId).toList();
        // 分批防止 IN 过大
        List<CandidateApplication> all = new ArrayList<>();
        for (int i = 0; i < ids.size(); i += 500) {
            List<Long> batch = ids.subList(i, Math.min(i + 500, ids.size()));
            all.addAll(applicationMapper.selectList(
                    new QueryWrapper<CandidateApplication>().in("candidate_id", batch)));
        }
        return all;
    }

    private static long count(Long n) {
        return n == null ? 0L : n;
    }
}
