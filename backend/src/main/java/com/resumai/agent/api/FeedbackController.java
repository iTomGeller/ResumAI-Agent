package com.resumai.agent.api;

import com.resumai.agent.api.dto.FeedbackRequest;
import com.resumai.agent.api.dto.FeedbackResponse;
import com.resumai.agent.service.MvpEvaluationService;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * HR 反馈控制器。
 */
@RestController
@RequestMapping("/api/feedback")
public class FeedbackController {

    private final MvpEvaluationService evaluationService;

    public FeedbackController(MvpEvaluationService evaluationService) {
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
    public List<FeedbackResponse> listFeedbacks() {
        return evaluationService.listFeedbacks();
    }
}
