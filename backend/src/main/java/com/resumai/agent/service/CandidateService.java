package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.toolkit.IdWorker;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.resumai.agent.api.dto.CandidateApplicationResponse;
import com.resumai.agent.api.dto.CandidateAssessmentResponse;
import com.resumai.agent.api.dto.CandidateDetailResponse;
import com.resumai.agent.api.dto.CandidateListItemResponse;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.PatchApplicationRequest;
import com.resumai.agent.dao.CandidateApplicationMapper;
import com.resumai.agent.dao.CandidateProfileMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.CandidateApplication;
import com.resumai.agent.domain.entity.CandidateProfile;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.DataOrigin;
import com.resumai.agent.service.candidate.CandidateIdentityExtractor;
import com.resumai.agent.service.candidate.IdentityHints;
import com.resumai.agent.util.HrContext;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

/**
 * 候选人域：按身份启发 upsert 档案、按 application_key 复用投递，并提供列表/详情/评估查询。
 */
@Service
public class CandidateService {

    private static final Logger log = LoggerFactory.getLogger(CandidateService.class);

    private static final Set<String> ALLOWED_STAGES = Set.of(
            "NEW", "SCREENING", "INTERVIEW", "OFFER", "REJECTED", "HIRED");

    private final CandidateProfileMapper profileMapper;
    private final CandidateApplicationMapper applicationMapper;
    private final ResumeTaskMapper resumeTaskMapper;
    private final CandidateIdentityExtractor identityExtractor;

    public CandidateService(CandidateProfileMapper profileMapper,
                            CandidateApplicationMapper applicationMapper,
                            ResumeTaskMapper resumeTaskMapper,
                            CandidateIdentityExtractor identityExtractor) {
        this.profileMapper = profileMapper;
        this.applicationMapper = applicationMapper;
        this.resumeTaskMapper = resumeTaskMapper;
        this.identityExtractor = identityExtractor;
    }

    /** 上传/建任务时：upsert 候选人并按 application_key 复用/新建 application。 */
    @Transactional
    public CandidateLink upsertOnTaskCreate(String tenantId,
                                            String resumeText,
                                            String fileName,
                                            String jobCategory,
                                            String jobId,
                                            Long taskId,
                                            String traceId) {
        String tenant = StringUtils.hasText(tenantId) ? tenantId : "default";
        IdentityHints hints = identityExtractor.extract(resumeText, fileName, traceId);
        CandidateProfile profile = findOrCreateProfile(tenant, hints, DataOrigin.USER_UPLOAD);

        String applicationKey = buildApplicationKey(profile.getId(), jobId, jobCategory);
        LocalDateTime now = LocalDateTime.now();
        CandidateApplication app = applicationMapper.selectOne(
                new QueryWrapper<CandidateApplication>()
                        .eq("tenant_id", tenant)
                        .eq("application_key", applicationKey)
                        .last("LIMIT 1"));
        if (app == null) {
            app = new CandidateApplication();
            app.setId(IdWorker.getId());
            app.setCandidateId(profile.getId());
            app.setTenantId(tenant);
            app.setJobCategory(jobCategory);
            app.setJobId(jobId);
            app.setApplicationKey(applicationKey);
            app.setStage("NEW");
            app.setOwnerHrId(HrContext.getHrId());
            app.setLatestTaskId(taskId);
            app.setLatestTraceId(traceId);
            app.setSourceFileName(fileName);
            app.setCreateTime(now);
            app.setUpdateTime(now);
            app.setDeleted(0);
            try {
                applicationMapper.insert(app);
            } catch (Exception e) {
                CandidateApplication raced = applicationMapper.selectOne(
                        new QueryWrapper<CandidateApplication>()
                                .eq("tenant_id", tenant)
                                .eq("application_key", applicationKey)
                                .last("LIMIT 1"));
                if (raced == null) {
                    throw e;
                }
                app = raced;
                touchApplication(app, taskId, traceId, fileName, now);
            }
        } else {
            touchApplication(app, taskId, traceId, fileName, now);
        }

        if (StringUtils.hasText(hints.displayName())
                && !hints.displayName().equals(profile.getDisplayName())) {
            profile.setDisplayName(hints.displayName());
            profile.setUpdateTime(now);
            profileMapper.updateById(profile);
        }

        return new CandidateLink(profile.getId(), app.getId(), profile.getDisplayName());
    }

    private void touchApplication(CandidateApplication app,
                                  Long taskId,
                                  String traceId,
                                  String fileName,
                                  LocalDateTime now) {
        app.setLatestTaskId(taskId);
        app.setLatestTraceId(traceId);
        if (StringUtils.hasText(fileName)) {
            app.setSourceFileName(fileName);
        }
        app.setUpdateTime(now);
        applicationMapper.updateById(app);
    }

    public static String buildApplicationKey(Long candidateId, String jobId, String jobCategory) {
        String job = normalizedJob(jobId, jobCategory);
        return candidateId + ":" + job;
    }

    public static String normalizedJob(String jobId, String jobCategory) {
        if (StringUtils.hasText(jobId)) {
            return jobId.trim();
        }
        if (StringUtils.hasText(jobCategory)) {
            return jobCategory.trim().toUpperCase(Locale.ROOT);
        }
        return "UNKNOWN";
    }

    public PageResult<CandidateListItemResponse> listCandidates(String keyword,
                                                                String stage,
                                                                int page,
                                                                int pageSize) {
        int safePage = Math.max(1, page);
        int safeSize = Math.min(100, Math.max(1, pageSize));
        String wantStage = StringUtils.hasText(stage) && !"ALL".equalsIgnoreCase(stage)
                ? stage.trim().toUpperCase(Locale.ROOT) : null;

        Set<Long> stageCandidateIds = null;
        if (wantStage != null) {
            List<CandidateApplication> staged = applicationMapper.selectList(
                    new QueryWrapper<CandidateApplication>().eq("stage", wantStage).select("candidate_id"));
            stageCandidateIds = new LinkedHashSet<>();
            for (CandidateApplication a : staged) {
                if (a.getCandidateId() != null) {
                    stageCandidateIds.add(a.getCandidateId());
                }
            }
            if (stageCandidateIds.isEmpty()) {
                return PageResult.of(List.of(), 0, safePage, safeSize);
            }
        }

        QueryWrapper<CandidateProfile> qw = new QueryWrapper<>();
        // HR 列表默认排除 benchmark/acceptance 噪声
        qw.and(w -> w.eq("data_origin", DataOrigin.USER_UPLOAD.name())
                .or().isNull("data_origin"));
        if (stageCandidateIds != null) {
            qw.in("id", stageCandidateIds);
        }
        if (StringUtils.hasText(keyword)) {
            String kw = keyword.trim();
            qw.and(w -> w.like("display_name", kw)
                    .or().like("email", kw)
                    .or().like("phone", kw));
        }
        qw.orderByDesc("update_time");
        Page<CandidateProfile> mp = profileMapper.selectPage(new Page<>(safePage, safeSize), qw);
        List<CandidateListItemResponse> items = new ArrayList<>();
        for (CandidateProfile p : mp.getRecords()) {
            items.add(toListItem(p));
        }
        return PageResult.of(items, mp.getTotal(), safePage, safeSize);
    }

    public CandidateDetailResponse getCandidate(Long id) {
        CandidateProfile profile = profileMapper.selectById(id);
        if (profile == null) {
            throw new IllegalArgumentException("候选人不存在: " + id);
        }
        List<CandidateApplication> apps = applicationMapper.selectList(
                new QueryWrapper<CandidateApplication>()
                        .eq("candidate_id", id)
                        .orderByDesc("create_time"));
        List<CandidateApplicationResponse> appResponses = apps.stream().map(this::toAppResponse).toList();
        return new CandidateDetailResponse(
                profile.getId(),
                profile.getDisplayName(),
                profile.getEmail(),
                profile.getPhone(),
                profile.getIdentityKey(),
                profile.getIdentitySource(),
                profile.getResumeFingerprint(),
                profile.getCreateTime(),
                profile.getUpdateTime(),
                appResponses);
    }

    public PageResult<CandidateAssessmentResponse> listAssessments(Long candidateId,
                                                                   int page,
                                                                   int pageSize) {
        CandidateProfile profile = profileMapper.selectById(candidateId);
        if (profile == null) {
            throw new IllegalArgumentException("候选人不存在: " + candidateId);
        }
        int safePage = Math.max(1, page);
        int safeSize = Math.min(100, Math.max(1, pageSize));
        QueryWrapper<ResumeTask> qw = new QueryWrapper<>();
        qw.eq("candidate_id", candidateId).orderByDesc("create_time");
        Page<ResumeTask> mp = resumeTaskMapper.selectPage(new Page<>(safePage, safeSize), qw);
        List<CandidateAssessmentResponse> items = mp.getRecords().stream()
                .map(t -> new CandidateAssessmentResponse(
                        t.getId(),
                        t.getTraceId(),
                        t.getApplicationId(),
                        t.getFileName(),
                        t.getJobCategory(),
                        t.getStatus(),
                        t.getOverallScore(),
                        t.getRecommendation(),
                        t.getSummary(),
                        t.getDurationMs(),
                        t.getCreateTime(),
                        t.getUpdateTime()))
                .toList();
        return PageResult.of(items, mp.getTotal(), safePage, safeSize);
    }

    @Transactional
    public CandidateApplicationResponse patchApplication(Long applicationId, PatchApplicationRequest request) {
        CandidateApplication app = applicationMapper.selectById(applicationId);
        if (app == null) {
            throw new IllegalArgumentException("投递申请不存在: " + applicationId);
        }
        boolean changed = false;
        if (request != null && StringUtils.hasText(request.stage())) {
            String stage = request.stage().trim().toUpperCase(Locale.ROOT);
            if (!ALLOWED_STAGES.contains(stage)) {
                throw new IllegalArgumentException("非法 stage: " + request.stage());
            }
            app.setStage(stage);
            changed = true;
        }
        if (request != null && request.ownerHrId() != null) {
            app.setOwnerHrId(request.ownerHrId().isBlank() ? null : request.ownerHrId().trim());
            changed = true;
        }
        if (changed) {
            app.setUpdateTime(LocalDateTime.now());
            applicationMapper.updateById(app);
        }
        return toAppResponse(app);
    }

    /** 评估完成后回写申请上的最新分数/推荐。 */
    public void syncApplicationFromTask(Long applicationId,
                                        Long taskId,
                                        String traceId,
                                        Integer score,
                                        String recommendation) {
        if (applicationId == null) {
            return;
        }
        try {
            CandidateApplication app = applicationMapper.selectById(applicationId);
            if (app == null) {
                return;
            }
            app.setLatestTaskId(taskId);
            app.setLatestTraceId(traceId);
            app.setLatestScore(score);
            app.setLatestRecommendation(recommendation);
            if ("NEW".equals(app.getStage()) && score != null) {
                app.setStage("SCREENING");
            }
            app.setUpdateTime(LocalDateTime.now());
            applicationMapper.updateById(app);
        } catch (Exception e) {
            log.warn("[candidate] syncApplicationFromTask failed app={}: {}", applicationId, e.getMessage());
        }
    }

    public int countUniqueCandidates() {
        try {
            Long n = profileMapper.selectCount(new QueryWrapper<CandidateProfile>()
                    .and(w -> w.eq("data_origin", DataOrigin.USER_UPLOAD.name())
                            .or().isNull("data_origin")));
            return n == null ? 0 : n.intValue();
        } catch (Exception e) {
            log.warn("[candidate] countUniqueCandidates failed: {}", e.getMessage());
            return 0;
        }
    }

    public CandidateProfile findOrCreateProfile(String tenant, IdentityHints hints, DataOrigin origin) {
        CandidateProfile existing = profileMapper.selectOne(
                new QueryWrapper<CandidateProfile>()
                        .eq("tenant_id", tenant)
                        .eq("identity_key", hints.identityKey())
                        .last("LIMIT 1"));
        if (existing != null) {
            boolean dirty = false;
            if (!StringUtils.hasText(existing.getEmail()) && StringUtils.hasText(hints.email())) {
                existing.setEmail(hints.email());
                dirty = true;
            }
            if (!StringUtils.hasText(existing.getPhone()) && StringUtils.hasText(hints.phone())) {
                existing.setPhone(hints.phone());
                dirty = true;
            }
            if (!StringUtils.hasText(existing.getDisplayName()) && StringUtils.hasText(hints.displayName())) {
                existing.setDisplayName(hints.displayName());
                dirty = true;
            }
            if (origin == DataOrigin.USER_UPLOAD && !DataOrigin.USER_UPLOAD.name().equals(existing.getDataOrigin())) {
                existing.setDataOrigin(origin.name());
                dirty = true;
            }
            if (dirty) {
                existing.setUpdateTime(LocalDateTime.now());
                profileMapper.updateById(existing);
            }
            return existing;
        }

        LocalDateTime now = LocalDateTime.now();
        CandidateProfile created = new CandidateProfile();
        created.setId(IdWorker.getId());
        created.setTenantId(tenant);
        created.setDisplayName(hints.displayName());
        created.setEmail(hints.email());
        created.setPhone(hints.phone());
        created.setIdentityKey(hints.identityKey());
        created.setIdentitySource(hints.identitySource());
        created.setResumeFingerprint(hints.resumeFingerprint());
        created.setIdentityConfidence(BigDecimal.valueOf(hints.identityConfidence())
                .setScale(3, RoundingMode.HALF_UP));
        created.setNeedsMergeReview(hints.needsMergeReview() ? 1 : 0);
        created.setDataOrigin(origin == null ? DataOrigin.USER_UPLOAD.name() : origin.name());
        created.setCreateTime(now);
        created.setUpdateTime(now);
        created.setDeleted(0);
        try {
            profileMapper.insert(created);
            return created;
        } catch (Exception e) {
            CandidateProfile raced = profileMapper.selectOne(
                    new QueryWrapper<CandidateProfile>()
                            .eq("tenant_id", tenant)
                            .eq("identity_key", hints.identityKey())
                            .last("LIMIT 1"));
            if (raced != null) {
                return raced;
            }
            throw e;
        }
    }

    public CandidateApplication findOrCreateApplication(CandidateProfile profile, ResumeTask task) {
        String tenant = StringUtils.hasText(profile.getTenantId()) ? profile.getTenantId() : "default";
        String applicationKey = buildApplicationKey(profile.getId(), task.getJobId(), task.getJobCategory());
        CandidateApplication app = applicationMapper.selectOne(
                new QueryWrapper<CandidateApplication>()
                        .eq("tenant_id", tenant)
                        .eq("application_key", applicationKey)
                        .last("LIMIT 1"));
        LocalDateTime now = LocalDateTime.now();
        if (app != null) {
            app.setLatestTaskId(task.getId());
            app.setLatestTraceId(task.getTraceId());
            if (task.getOverallScore() != null) {
                app.setLatestScore(task.getOverallScore());
            }
            if (StringUtils.hasText(task.getRecommendation())) {
                app.setLatestRecommendation(task.getRecommendation());
            }
            if (StringUtils.hasText(task.getFileName())) {
                app.setSourceFileName(task.getFileName());
            }
            app.setUpdateTime(now);
            applicationMapper.updateById(app);
            return app;
        }
        app = new CandidateApplication();
        app.setId(IdWorker.getId());
        app.setCandidateId(profile.getId());
        app.setTenantId(tenant);
        app.setJobCategory(task.getJobCategory());
        app.setJobId(task.getJobId());
        app.setApplicationKey(applicationKey);
        app.setStage(task.getOverallScore() != null ? "SCREENING" : "NEW");
        app.setLatestTaskId(task.getId());
        app.setLatestTraceId(task.getTraceId());
        app.setLatestScore(task.getOverallScore());
        app.setLatestRecommendation(task.getRecommendation());
        app.setSourceFileName(task.getFileName());
        app.setCreateTime(now);
        app.setUpdateTime(now);
        app.setDeleted(0);
        try {
            applicationMapper.insert(app);
            return app;
        } catch (Exception e) {
            CandidateApplication raced = applicationMapper.selectOne(
                    new QueryWrapper<CandidateApplication>()
                            .eq("tenant_id", tenant)
                            .eq("application_key", applicationKey)
                            .last("LIMIT 1"));
            if (raced != null) {
                return raced;
            }
            throw e;
        }
    }

    /** 兼容旧测试：委托给 extractor。 */
    public static IdentityHints extractIdentity(String resumeText, String fileName) {
        return new CandidateIdentityExtractor().extract(resumeText, fileName, null);
    }

    public static IdentityHints extractIdentity(String resumeText, String fileName, String traceId) {
        return new CandidateIdentityExtractor().extract(resumeText, fileName, traceId);
    }

    private CandidateListItemResponse toListItem(CandidateProfile p) {
        List<CandidateApplication> apps = applicationMapper.selectList(
                new QueryWrapper<CandidateApplication>()
                        .eq("candidate_id", p.getId())
                        .orderByDesc("update_time")
                        .last("LIMIT 50"));
        CandidateApplication latest = apps.isEmpty() ? null : apps.get(0);
        Long assessmentCount = resumeTaskMapper.selectCount(
                new QueryWrapper<ResumeTask>().eq("candidate_id", p.getId()));
        Set<Long> appIds = new LinkedHashSet<>();
        for (CandidateApplication a : apps) {
            appIds.add(a.getId());
        }
        return new CandidateListItemResponse(
                p.getId(),
                p.getDisplayName(),
                p.getEmail(),
                p.getPhone(),
                p.getIdentitySource(),
                appIds.size(),
                assessmentCount == null ? 0 : assessmentCount.intValue(),
                latest != null ? latest.getStage() : null,
                latest != null ? latest.getOwnerHrId() : null,
                latest != null ? latest.getLatestScore() : null,
                latest != null ? latest.getLatestRecommendation() : null,
                latest != null ? latest.getJobCategory() : null,
                latest != null ? latest.getLatestTraceId() : null,
                p.getCreateTime(),
                p.getUpdateTime());
    }

    private CandidateApplicationResponse toAppResponse(CandidateApplication a) {
        return new CandidateApplicationResponse(
                a.getId(),
                a.getCandidateId(),
                a.getJobCategory(),
                a.getJobId(),
                a.getStage(),
                a.getOwnerHrId(),
                a.getLatestTaskId(),
                a.getLatestTraceId(),
                a.getLatestScore(),
                a.getLatestRecommendation(),
                a.getSourceFileName(),
                a.getCreateTime(),
                a.getUpdateTime());
    }

    public record CandidateLink(Long candidateId, Long applicationId, String displayName) {
    }

    /** @deprecated 使用 {@link IdentityHints}；保留别名避免外部编译断裂。 */
    @Deprecated
    public record LegacyIdentityHints(
            String displayName,
            String email,
            String phone,
            String identityKey,
            String identitySource,
            String resumeFingerprint
    ) {
        public static LegacyIdentityHints from(IdentityHints h) {
            return new LegacyIdentityHints(
                    h.displayName(), h.email(), h.phone(),
                    h.identityKey(), h.identitySource(), h.resumeFingerprint());
        }
    }
}
