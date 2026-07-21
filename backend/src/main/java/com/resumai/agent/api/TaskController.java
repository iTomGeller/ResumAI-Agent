package com.resumai.agent.api;

import com.resumai.agent.api.dto.CreateTaskRequest;
import com.resumai.agent.api.dto.DashboardMetricsResponse;
import com.resumai.agent.api.dto.JdDetailResponse;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.api.dto.JdSummaryResponse;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.TaskListItemResponse;
import com.resumai.agent.api.dto.TaskResponse;
import com.resumai.agent.api.dto.UpsertJdRequest;
import com.resumai.agent.service.JdRagService;
import com.resumai.agent.service.ResumeEvaluationService;
import com.resumai.agent.service.run.RunTraceBridgeService;
import jakarta.validation.Valid;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api")
public class TaskController {

    private final ResumeEvaluationService evaluationService;
    private final JdRagService jdRagService;
    private final RunTraceBridgeService traceBridge;

    public TaskController(ResumeEvaluationService evaluationService,
                          JdRagService jdRagService,
                          RunTraceBridgeService traceBridge) {
        this.evaluationService = evaluationService;
        this.jdRagService = jdRagService;
        this.traceBridge = traceBridge;
    }

    @PostMapping("/tasks")
    public TaskResponse createTask(@Valid @RequestBody CreateTaskRequest request) {
        return evaluationService.createTask(request);
    }

    @PostMapping("/tasks/upload")
    public TaskResponse uploadTask(@RequestParam("file") MultipartFile file,
                                   @RequestParam("jobCategory") String jobCategory,
                                   @RequestParam("executionMode") String executionMode,
                                   @RequestParam("jobDescription") String jobDescription,
                                   @RequestParam(value = "planMode", defaultValue = "false") boolean planMode) {
        return evaluationService.createTaskFromUpload(file, jobCategory, executionMode, jobDescription, planMode);
    }

    @PostMapping("/tasks/upload-auto")
    public TaskResponse uploadTaskAutoMatch(@RequestParam("file") MultipartFile file,
                                            @RequestParam(value = "executionMode", defaultValue = "DAG_CONCURRENT") String executionMode,
                                            @RequestParam(value = "planMode", defaultValue = "false") boolean planMode) {
        return evaluationService.createTaskFromUploadAutoMatch(file, executionMode, planMode);
    }

    @GetMapping("/tasks")
    public PageResult<TaskListItemResponse> listTasks(
            @RequestParam(required = false) String keyword,
            @RequestParam(required = false, defaultValue = "ALL") String status,
            @RequestParam(required = false, defaultValue = "ALL") String recommendation,
            @RequestParam(required = false, defaultValue = "ALL") String jobCategory,
            @RequestParam(required = false, defaultValue = "ALL") String queueStatus,
            @RequestParam(required = false, defaultValue = "ALL") String uploadedBy,
            @RequestParam(required = false) Integer scoreMin,
            @RequestParam(required = false) Integer scoreMax,
            @RequestParam(required = false, defaultValue = "create_time") String sortBy,
            @RequestParam(required = false, defaultValue = "desc") String sortOrder,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        return evaluationService.queryTasks(keyword, status, recommendation, jobCategory, queueStatus, uploadedBy,
                scoreMin, scoreMax, sortBy, sortOrder, page, pageSize);
    }

    @GetMapping("/tasks/{traceId}")
    public TaskResponse getTask(@PathVariable String traceId) {
        return evaluationService.getTask(traceId);
    }

    @DeleteMapping("/tasks/{traceId}")
    public Map<String, String> deleteTask(@PathVariable String traceId) {
        evaluationService.deleteTask(traceId);
        return Map.of("status", "deleted", "traceId", traceId);
    }

    @PostMapping("/tasks/batch-delete")
    public Map<String, Object> batchDeleteTasks(@RequestBody Map<String, List<String>> body) {
        List<String> traceIds = body.getOrDefault("traceIds", List.of());
        if (!traceIds.isEmpty()) {
            evaluationService.deleteTasks(traceIds);
        }
        return Map.of("status", "deleted", "count", traceIds.size());
    }

    @GetMapping("/tasks/{traceId}/file")
    public ResponseEntity<Resource> getTaskFile(@PathVariable String traceId) {
        Path filePath = evaluationService.getResumeFile(traceId);
        if (filePath == null || !Files.isRegularFile(filePath)) {
            return ResponseEntity.notFound().build();
        }
        String contentType;
        try {
            contentType = Files.probeContentType(filePath);
        } catch (IOException e) {
            contentType = null;
        }
        if (contentType == null) {
            contentType = filePath.toString().toLowerCase().endsWith(".pdf")
                    ? MediaType.APPLICATION_PDF_VALUE
                    : MediaType.TEXT_PLAIN_VALUE;
        }
        Resource resource = new FileSystemResource(filePath);
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "inline; filename=\"" + filePath.getFileName() + "\"")
                .contentType(MediaType.parseMediaType(contentType))
                .body(resource);
    }

    @GetMapping("/metrics")
    public DashboardMetricsResponse metrics() {
        return evaluationService.metrics();
    }

    /** @deprecated 使用 POST /api/jds 或 PUT /api/jds/{jdId} */
    @PostMapping("/jd")
    public Map<String, String> indexJd(@RequestBody Map<String, String> body) {
        String jdId = body.getOrDefault("jdId", "jd-" + System.currentTimeMillis());
        String title = body.getOrDefault("title", "");
        String category = body.getOrDefault("category", "TECH");
        String description = body.getOrDefault("description", "");
        jdRagService.indexJd(jdId, title, category, description);
        return Map.of("status", "indexed", "jdId", jdId);
    }

    @PostMapping("/jds")
    public JdDetailResponse createJd(@Valid @RequestBody UpsertJdRequest request) {
        return jdRagService.createJd(request);
    }

    @PutMapping("/jds/{jdId}")
    public JdDetailResponse updateJd(@PathVariable String jdId, @Valid @RequestBody UpsertJdRequest request) {
        return jdRagService.updateJd(jdId, request);
    }

    @GetMapping("/jds")
    public PageResult<JdSummaryResponse> listJds(
            @RequestParam(required = false) String keyword,
            @RequestParam(required = false, defaultValue = "ALL") String category,
            @RequestParam(required = false, defaultValue = "create_time") String sortBy,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(defaultValue = "20") int pageSize) {
        return jdRagService.queryJds(keyword, category, sortBy, page, pageSize);
    }

    @GetMapping("/jds/{jdId}")
    public JdDetailResponse getJd(@PathVariable String jdId) {
        return jdRagService.getJdDetail(jdId);
    }

    @DeleteMapping("/jds/{jdId}")
    public Map<String, String> deleteJd(@PathVariable String jdId) {
        jdRagService.deleteJd(jdId);
        return Map.of("status", "deleted", "jdId", jdId);
    }

    @PostMapping("/jd/match")
    public List<JdMatchResult> matchJds(@RequestBody Map<String, String> body) {
        String resumeText = body.getOrDefault("resumeText", "");
        return jdRagService.matchTopJds(resumeText, 3);
    }

    @GetMapping("/tasks/{traceId}/agent-execution")
    public Map<String, Object> getAgentExecution(@PathVariable String traceId) {
        Map<String, Object> legacy = evaluationService.getAgentExecutionTree(traceId);
        Object tree = legacy.get("executionTree");
        if (tree instanceof java.util.List<?> list && !list.isEmpty()) {
            return legacy;
        }
        return traceBridge.executionTreeForTrace(traceId);
    }
}
