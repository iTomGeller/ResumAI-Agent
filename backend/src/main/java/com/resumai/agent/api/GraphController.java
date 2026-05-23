package com.resumai.agent.api;

import com.resumai.agent.api.dto.GraphResponse;
import com.resumai.agent.service.MvpEvaluationService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * GraphRAG 图谱控制器。
 */
@RestController
@RequestMapping("/api/graphs")
public class GraphController {

    private final MvpEvaluationService evaluationService;

    public GraphController(MvpEvaluationService evaluationService) {
        this.evaluationService = evaluationService;
    }

    /**
     * 查询候选人 GraphRAG 子图。
     *
     * @param traceId TraceId
     * @return 图谱响应
     */
    @GetMapping("/{traceId}")
    public GraphResponse graph(@PathVariable String traceId) {
        return evaluationService.graph(traceId);
    }
}
