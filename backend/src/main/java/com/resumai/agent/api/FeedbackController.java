package com.resumai.agent.api;

import com.resumai.agent.api.dto.FeedbackRequest;
import com.resumai.agent.api.dto.FeedbackResponse;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.service.ResumeEvaluationService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * HR 反馈控制器。
 */
@RestController
@RequestMapping("/api/feedback")
public class FeedbackController {

    private final ResumeEvaluationService evaluationService;

    public FeedbackController(ResumeEvaluationService evaluationService) {
        this.evaluationService = evaluationService;
    }

    /**
     * 新增 HR 反馈。
     *
     * @param request 反馈请求
     * @return 反馈响应
     */
    @PostMapping
    public FeedbackResponse addFeedback(@Valid @RequestBody FeedbackRequest request) {
        return evaluationService.addFeedback(request);
    }

    /**
     * 查询 HR 反馈列表。
     *
     * @return 反馈列表
     */
    @GetMapping
    public PageResult<FeedbackResponse> listFeedbacks(
            @RequestParam(required = false) String traceId,
            @RequestParam(required = false) String feedbackType,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        return evaluationService.queryFeedbacks(traceId, feedbackType, page, pageSize);
    }
}
