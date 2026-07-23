package com.resumai.agent.api.dev;

import com.resumai.agent.api.dto.CandidateBackfillReport;
import com.resumai.agent.api.dto.CandidateDomainStats;
import com.resumai.agent.service.candidate.CandidateBackfillService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** 开发者：候选人域数据完整性与历史回填。 */
@RestController
@RequestMapping("/api/dev/data-integrity/candidates")
public class CandidateDataIntegrityController {

    private final CandidateBackfillService backfillService;

    public CandidateDataIntegrityController(CandidateBackfillService backfillService) {
        this.backfillService = backfillService;
    }

    @GetMapping
    public CandidateDomainStats stats() {
        return backfillService.stats();
    }

    @PostMapping("/backfill")
    public CandidateBackfillReport backfill(
            @RequestParam(defaultValue = "true") boolean dryRun,
            @RequestParam(defaultValue = "50") int batchSize) {
        return dryRun ? backfillService.dryRun(batchSize) : backfillService.apply(batchSize);
    }
}
