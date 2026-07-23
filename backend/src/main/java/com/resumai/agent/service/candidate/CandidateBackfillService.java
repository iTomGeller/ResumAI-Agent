package com.resumai.agent.service.candidate;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.resumai.agent.api.dto.CandidateBackfillReport;
import com.resumai.agent.api.dto.CandidateDomainStats;
import com.resumai.agent.dao.CandidateApplicationMapper;
import com.resumai.agent.dao.CandidateBackfillLedgerMapper;
import com.resumai.agent.dao.CandidateProfileMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.CandidateApplication;
import com.resumai.agent.domain.entity.CandidateBackfillLedger;
import com.resumai.agent.domain.entity.CandidateProfile;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.DataOrigin;
import com.resumai.agent.service.CandidateService;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.util.StringUtils;

/**
 * 历史任务回填候选人域：dryRun 预览 / apply 幂等写入。
 * 仅处理 candidate_id IS NULL；ledger + 乐观 WHERE 保证并发安全。
 */
@Service
public class CandidateBackfillService {

    private static final Logger log = LoggerFactory.getLogger(CandidateBackfillService.class);

    private final ResumeTaskMapper resumeTaskMapper;
    private final CandidateProfileMapper profileMapper;
    private final CandidateApplicationMapper applicationMapper;
    private final CandidateBackfillLedgerMapper ledgerMapper;
    private final CandidateIdentityExtractor identityExtractor;
    private final OriginClassifier originClassifier;
    private final CandidateService candidateService;
    private final TransactionTemplate txTemplate;

    public CandidateBackfillService(ResumeTaskMapper resumeTaskMapper,
                                    CandidateProfileMapper profileMapper,
                                    CandidateApplicationMapper applicationMapper,
                                    CandidateBackfillLedgerMapper ledgerMapper,
                                    CandidateIdentityExtractor identityExtractor,
                                    OriginClassifier originClassifier,
                                    CandidateService candidateService,
                                    PlatformTransactionManager txManager) {
        this.resumeTaskMapper = resumeTaskMapper;
        this.profileMapper = profileMapper;
        this.applicationMapper = applicationMapper;
        this.ledgerMapper = ledgerMapper;
        this.identityExtractor = identityExtractor;
        this.originClassifier = originClassifier;
        this.candidateService = candidateService;
        this.txTemplate = new TransactionTemplate(txManager);
    }

    public CandidateBackfillReport dryRun(int batchSize) {
        return run(true, batchSize);
    }

    public CandidateBackfillReport apply(int batchSize) {
        return run(false, batchSize);
    }

    public CandidateBackfillReport run(boolean dryRun, int batchSize) {
        int size = clamp(batchSize, 1, 500);
        List<ResumeTask> tasks = resumeTaskMapper.selectList(
                new QueryWrapper<ResumeTask>()
                        .isNull("candidate_id")
                        .orderByAsc("id")
                        .last("LIMIT " + size));

        ReportBuilder report = new ReportBuilder(dryRun, size);
        for (ResumeTask task : tasks) {
            DataOrigin origin = originClassifier.classify(task);
            IdentityHints hints = identityExtractor.extract(
                    task.getResumeText(), task.getFileName(), task.getTraceId());
            report.observe(task, origin, hints);
            if (dryRun) {
                continue;
            }
            try {
                txTemplate.executeWithoutResult(status -> linkOne(task, origin, hints, report));
            } catch (Exception e) {
                log.warn("[backfill] link failed task={}: {}", task.getId(), e.getMessage());
                report.failed++;
                try {
                    if (ledgerMapper.selectById(task.getId()) == null) {
                        ledgerMapper.insert(CandidateBackfillLedger.failed(task.getId(), e.getMessage()));
                    }
                } catch (Exception ignore) {
                    // ignore ledger race
                }
            }
        }
        return report.build();
    }

    void linkOne(ResumeTask task,
                 DataOrigin origin,
                 IdentityHints hints,
                 ReportBuilder report) {
        if (ledgerMapper.selectById(task.getId()) != null) {
            report.skippedAlreadyLinked++;
            return;
        }

        String tenant = StringUtils.hasText(task.getTenantId()) ? task.getTenantId() : "default";
        boolean profileExisted = profileMapper.selectOne(
                new QueryWrapper<CandidateProfile>()
                        .eq("tenant_id", tenant)
                        .eq("identity_key", hints.identityKey())
                        .last("LIMIT 1")) != null;

        CandidateProfile profile = candidateService.findOrCreateProfile(tenant, hints, origin);
        if (profileExisted) {
            report.reusedProfile++;
        } else {
            report.createdProfile++;
        }

        String appKey = CandidateService.buildApplicationKey(
                profile.getId(), task.getJobId(), task.getJobCategory());
        boolean appExisted = applicationMapper.selectOne(
                new QueryWrapper<CandidateApplication>()
                        .eq("tenant_id", tenant)
                        .eq("application_key", appKey)
                        .last("LIMIT 1")) != null;

        CandidateApplication app = candidateService.findOrCreateApplication(profile, task);
        if (appExisted) {
            report.reusedApplication++;
        } else {
            report.createdApplication++;
        }

        int changed = resumeTaskMapper.update(
                null,
                new UpdateWrapper<ResumeTask>()
                        .eq("id", task.getId())
                        .isNull("candidate_id")
                        .set("candidate_id", profile.getId())
                        .set("application_id", app.getId())
                        .set("candidate_link_status", "LINKED")
                        .set("candidate_link_reason", hints.identitySource())
                        .set("data_origin", origin.name()));
        if (changed == 0) {
            report.skippedAlreadyLinked++;
            return;
        }

        ledgerMapper.insert(CandidateBackfillLedger.linked(
                task.getId(), profile.getId(), app.getId(), hints.identityKey()));
        candidateService.syncApplicationFromTask(
                app.getId(), task.getId(), task.getTraceId(),
                task.getOverallScore(), task.getRecommendation());
        report.linked++;
    }

    public CandidateDomainStats stats() {
        long activeTasks = count(new QueryWrapper<ResumeTask>());
        long linkedTasks = count(new QueryWrapper<ResumeTask>().isNotNull("candidate_id"));
        long unlinkedTasks = count(new QueryWrapper<ResumeTask>().isNull("candidate_id"));
        long skippedTasks = count(new QueryWrapper<ResumeTask>()
                .eq("candidate_link_status", "SKIPPED"));
        long verifiedProfiles = countProfiles(new QueryWrapper<CandidateProfile>()
                .ne("identity_source", "LEGACY_UNVERIFIED")
                .ne("identity_source", "HASH"));
        long legacyUnverified = countProfiles(new QueryWrapper<CandidateProfile>()
                .and(w -> w.eq("identity_source", "LEGACY_UNVERIFIED")
                        .or().eq("identity_source", "HASH")
                        .or().eq("needs_merge_review", 1)));
        double ratio = activeTasks == 0 ? 1.0 : (double) linkedTasks / (double) activeTasks;
        String mode;
        if (linkedTasks == 0 && unlinkedTasks > 0) {
            mode = "LEGACY_TASKS";
        } else if (unlinkedTasks == 0) {
            mode = "CANDIDATE_ONLY";
        } else {
            mode = "DUAL";
        }
        return new CandidateDomainStats(
                activeTasks, linkedTasks, unlinkedTasks, skippedTasks,
                verifiedProfiles, legacyUnverified, round3(ratio), mode);
    }

    private long count(QueryWrapper<ResumeTask> qw) {
        Long n = resumeTaskMapper.selectCount(qw);
        return n == null ? 0L : n;
    }

    private long countProfiles(QueryWrapper<CandidateProfile> qw) {
        Long n = profileMapper.selectCount(qw);
        return n == null ? 0L : n;
    }

    private static int clamp(int v, int min, int max) {
        return Math.max(min, Math.min(max, v));
    }

    private static double round3(double v) {
        return Math.round(v * 1000.0) / 1000.0;
    }

    static final class ReportBuilder {
        final boolean dryRun;
        final int batchSize;
        int scanned;
        int wouldLink;
        int linked;
        int reusedProfile;
        int createdProfile;
        int reusedApplication;
        int createdApplication;
        int lowConfidence;
        int nonHrOrigin;
        int skippedAlreadyLinked;
        int failed;
        final List<Map<String, Object>> conflictsTopN = new ArrayList<>();
        final List<Map<String, Object>> samples = new ArrayList<>();

        ReportBuilder(boolean dryRun, int batchSize) {
            this.dryRun = dryRun;
            this.batchSize = batchSize;
        }

        void observe(ResumeTask task, DataOrigin origin, IdentityHints hints) {
            scanned++;
            if (!origin.isHrCohort()) {
                nonHrOrigin++;
            }
            if (hints.identityConfidence() < 0.5 || hints.needsMergeReview()) {
                lowConfidence++;
            }
            wouldLink++;
            if (samples.size() < 20) {
                Map<String, Object> sample = new LinkedHashMap<>();
                sample.put("taskId", task.getId());
                sample.put("traceId", task.getTraceId());
                sample.put("fileName", task.getFileName());
                sample.put("origin", origin.name());
                sample.put("identityKey", hints.identityKey());
                sample.put("identitySource", hints.identitySource());
                sample.put("displayName", hints.displayName());
                sample.put("confidence", hints.identityConfidence());
                samples.add(sample);
            }
            if (hints.needsMergeReview() && conflictsTopN.size() < 10) {
                Map<String, Object> c = new LinkedHashMap<>();
                c.put("taskId", task.getId());
                c.put("identityKey", hints.identityKey());
                c.put("reason", "LOW_CONFIDENCE_OR_LEGACY");
                c.put("displayName", hints.displayName());
                conflictsTopN.add(c);
            }
        }

        CandidateBackfillReport build() {
            return new CandidateBackfillReport(
                    dryRun, batchSize, scanned, wouldLink, linked,
                    reusedProfile, createdProfile, reusedApplication, createdApplication,
                    lowConfidence, nonHrOrigin, skippedAlreadyLinked, failed,
                    List.copyOf(conflictsTopN), List.copyOf(samples));
        }
    }
}
