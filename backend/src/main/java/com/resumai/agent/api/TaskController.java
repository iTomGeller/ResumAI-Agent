package com.resumai.agent.api;

import com.resumai.agent.api.dto.CreateTaskRequest;
import com.resumai.agent.api.dto.DashboardMetricsResponse;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.api.dto.TaskResponse;
import com.resumai.agent.service.JdRagService;
import com.resumai.agent.service.MvpEvaluationService;
import jakarta.validation.Valid;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api")
public class TaskController {

    private final MvpEvaluationService evaluationService;
    private final JdRagService jdRagService;

    public TaskController(MvpEvaluationService evaluationService, JdRagService jdRagService) {
        this.evaluationService = evaluationService;
        this.jdRagService = jdRagService;
    }

    @PostMapping("/tasks")
    public TaskResponse createTask(@Valid @RequestBody CreateTaskRequest request) {
        return evaluationService.createTask(request);
    }

    @PostMapping("/tasks/upload")
    public TaskResponse uploadTask(@RequestParam("file") MultipartFile file,
                                   @RequestParam("jobCategory") String jobCategory,
                                   @RequestParam("executionMode") String executionMode,
                                   @RequestParam("jobDescription") String jobDescription) {
        return evaluationService.createTaskFromUpload(file, jobCategory, executionMode, jobDescription);
    }

    /**
     * Upload resume with automatic JD matching via RAG.
     * No pre-selected JD needed -- the system finds the best match.
     */
    @PostMapping("/tasks/upload-auto")
    public TaskResponse uploadTaskAutoMatch(@RequestParam("file") MultipartFile file,
                                            @RequestParam(value = "executionMode", defaultValue = "DAG_CONCURRENT") String executionMode) {
        return evaluationService.createTaskFromUploadAutoMatch(file, executionMode);
    }

    @GetMapping("/tasks")
    public List<TaskResponse> listTasks() {
        return evaluationService.listTasks();
    }

    @GetMapping("/tasks/{traceId}")
    public TaskResponse getTask(@PathVariable String traceId) {
        return evaluationService.getTask(traceId);
    }

    @GetMapping("/metrics")
    public DashboardMetricsResponse metrics() {
        return evaluationService.metrics();
    }

    /**
     * Index a JD into the vector store for RAG matching.
     */
    @PostMapping("/jd")
    public Map<String, String> indexJd(@RequestBody Map<String, String> body) {
        String jdId = body.getOrDefault("jdId", "jd-" + System.currentTimeMillis());
        String title = body.getOrDefault("title", "");
        String category = body.getOrDefault("category", "TECH");
        String description = body.getOrDefault("description", "");
        jdRagService.indexJd(jdId, title, category, description);
        return Map.of("status", "indexed", "jdId", jdId);
    }

    /**
     * Match resume text against indexed JDs, returns top-3.
     */
    @PostMapping("/jd/match")
    public List<JdMatchResult> matchJds(@RequestBody Map<String, String> body) {
        String resumeText = body.getOrDefault("resumeText", "");
        return jdRagService.matchTopJds(resumeText, 3);
    }
}
