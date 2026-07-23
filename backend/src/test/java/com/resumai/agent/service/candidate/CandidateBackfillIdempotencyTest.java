package com.resumai.agent.service.candidate;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.baomidou.mybatisplus.core.conditions.Wrapper;
import com.resumai.agent.api.dto.CandidateBackfillReport;
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
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.SimpleTransactionStatus;

@ExtendWith(MockitoExtension.class)
class CandidateBackfillIdempotencyTest {

    @Mock ResumeTaskMapper resumeTaskMapper;
    @Mock CandidateProfileMapper profileMapper;
    @Mock CandidateApplicationMapper applicationMapper;
    @Mock CandidateBackfillLedgerMapper ledgerMapper;
    @Mock CandidateService candidateService;
    @Mock PlatformTransactionManager txManager;

    CandidateIdentityExtractor extractor = new CandidateIdentityExtractor();
    OriginClassifier originClassifier = new OriginClassifier();
    CandidateBackfillService service;

    @BeforeEach
    void setUp() {
        lenient().when(txManager.getTransaction(any(TransactionDefinition.class)))
                .thenReturn(new SimpleTransactionStatus());
        service = new CandidateBackfillService(
                resumeTaskMapper, profileMapper, applicationMapper, ledgerMapper,
                extractor, originClassifier, candidateService, txManager);
    }

    @Test
    void applyIsIdempotentWhenLedgerAlreadyExists() {
        ResumeTask task = unlinkedTask(101L, "trace-idem-1", "黄义健的简历.pdf",
                "姓名：黄义健\n邮箱：hyj@example.com\n");
        when(resumeTaskMapper.selectList(any())).thenReturn(List.of(task));
        when(ledgerMapper.selectById(101L)).thenReturn(CandidateBackfillLedger.linked(101L, 1L, 2L, "email:hyj@example.com"));

        CandidateBackfillReport report = service.apply(10);

        assertEquals(1, report.scanned());
        assertEquals(1, report.skippedAlreadyLinked());
        assertEquals(0, report.linked());
        verify(resumeTaskMapper, never()).update(any(), any());
        verify(ledgerMapper, never()).insert(any(CandidateBackfillLedger.class));
    }

    @Test
    void applyLinksOnceWithOptimisticCandidateIdNull() {
        ResumeTask task = unlinkedTask(202L, "trace-link-1", "黄义健的简历 (4).pdf",
                "姓名：黄义健\nhyj@example.com\n");
        when(resumeTaskMapper.selectList(any())).thenReturn(List.of(task));
        when(ledgerMapper.selectById(202L)).thenReturn(null);
        when(profileMapper.selectOne(any())).thenReturn(null);

        CandidateProfile profile = new CandidateProfile();
        profile.setId(9L);
        profile.setTenantId("default");
        when(candidateService.findOrCreateProfile(eq("default"), any(IdentityHints.class), eq(DataOrigin.USER_UPLOAD)))
                .thenReturn(profile);

        CandidateApplication app = new CandidateApplication();
        app.setId(8L);
        when(applicationMapper.selectOne(any())).thenReturn(null);
        when(candidateService.findOrCreateApplication(eq(profile), eq(task))).thenReturn(app);
        when(resumeTaskMapper.update(eq(null), any(Wrapper.class))).thenReturn(1);

        CandidateBackfillReport report = service.apply(10);

        assertEquals(1, report.linked());
        assertEquals(1, report.createdProfile());
        assertEquals(1, report.createdApplication());
        ArgumentCaptor<CandidateBackfillLedger> ledgerCap = ArgumentCaptor.forClass(CandidateBackfillLedger.class);
        verify(ledgerMapper, times(1)).insert(ledgerCap.capture());
        assertEquals(202L, ledgerCap.getValue().getTaskId());
        assertEquals("LINKED", ledgerCap.getValue().getAction());
        assertTrue(ledgerCap.getValue().getIdentityKey().startsWith("email:"));
    }

    @Test
    void originClassifierMarksBenchmark() {
        ResumeTask bench = unlinkedTask(1L, "bench-1", "benchmark_case_01.pdf", "x");
        bench.setUploadedBy("bench-runner");
        assertEquals(DataOrigin.BENCHMARK, originClassifier.classify(bench));

        ResumeTask accept = unlinkedTask(2L, "t", "accept_resume.pdf", "x");
        assertEquals(DataOrigin.ACCEPTANCE, originClassifier.classify(accept));
    }

    private static ResumeTask unlinkedTask(Long id, String traceId, String fileName, String text) {
        ResumeTask t = new ResumeTask();
        t.setId(id);
        t.setTraceId(traceId);
        t.setFileName(fileName);
        t.setResumeText(text);
        t.setTenantId("default");
        t.setJobCategory("TECH");
        t.setCandidateId(null);
        return t;
    }
}
