package com.resumai.agent.api;

import com.resumai.agent.api.dto.CandidateApplicationResponse;
import com.resumai.agent.api.dto.CandidateAssessmentResponse;
import com.resumai.agent.api.dto.CandidateDetailResponse;
import com.resumai.agent.api.dto.CandidateListItemResponse;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.PatchApplicationRequest;
import com.resumai.agent.service.CandidateService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/candidates")
public class CandidateController {

    private final CandidateService candidateService;

    public CandidateController(CandidateService candidateService) {
        this.candidateService = candidateService;
    }

    @GetMapping
    public PageResult<CandidateListItemResponse> list(
            @RequestParam(required = false) String keyword,
            @RequestParam(required = false, defaultValue = "ALL") String stage,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        return candidateService.listCandidates(keyword, stage, page, pageSize);
    }

    @GetMapping("/{id}")
    public CandidateDetailResponse get(@PathVariable Long id) {
        try {
            return candidateService.getCandidate(id);
        } catch (IllegalArgumentException e) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, e.getMessage());
        }
    }

    @GetMapping("/{id}/assessments")
    public PageResult<CandidateAssessmentResponse> assessments(
            @PathVariable Long id,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        try {
            return candidateService.listAssessments(id, page, pageSize);
        } catch (IllegalArgumentException e) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, e.getMessage());
        }
    }

    @PatchMapping("/applications/{applicationId}")
    public CandidateApplicationResponse patchApplication(
            @PathVariable Long applicationId,
            @RequestBody PatchApplicationRequest request) {
        try {
            return candidateService.patchApplication(applicationId, request);
        } catch (IllegalArgumentException e) {
            String msg = e.getMessage() == null ? "" : e.getMessage();
            HttpStatus status = msg.contains("不存在") ? HttpStatus.NOT_FOUND : HttpStatus.BAD_REQUEST;
            throw new ResponseStatusException(status, msg);
        }
    }
}
