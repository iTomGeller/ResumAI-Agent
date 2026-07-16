package com.resumai.agent.service;

import com.resumai.agent.api.dto.TaskListItemResponse;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;

@Service
public class RagAdvisorService {

    private static final int SAMPLE_SIZE = 20;
    private static final double REVIEW_THRESHOLD = 0.55;

    private final ResumeEvaluationService evaluationService;

    public RagAdvisorService(ResumeEvaluationService evaluationService) {
        this.evaluationService = evaluationService;
    }

    public Map<String, Object> suggest() {
        List<TaskListItemResponse> recent = evaluationService.queryTasks(
                null, "SUCCESS", null, null, null, null, null, null, "create_time", "desc", 1, SAMPLE_SIZE
        ).items();
        if (recent.size() < 5) {
            return Map.of("show", false);
        }
        long reviewCount = recent.stream()
                .filter(t -> t.recommendation() == null || !t.recommendation().contains("RECOMMEND"))
                .count();
        double reviewRate = (double) reviewCount / recent.size();
        if (reviewRate < REVIEW_THRESHOLD) {
            return Map.of("show", false, "reviewRate", reviewRate);
        }
        Map<String, Object> result = new HashMap<>();
        result.put("show", true);
        result.put("reviewRate", Math.round(reviewRate * 100));
        result.put("sampleSize", recent.size());
        result.put("message", String.format(
                "最近 %d 次评估，「需复核」占比 %d%%，可能召回过宽。建议切换到 🎯 严格匹配 预设，预计需复核率降至 ~35%%。",
                recent.size(), Math.round(reviewRate * 100)));
        result.put("suggestedPreset", "strict");
        result.put("suggestedPresetName", "严格匹配");
        return result;
    }
}
