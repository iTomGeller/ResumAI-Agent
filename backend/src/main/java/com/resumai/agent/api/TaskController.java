package com.resumai.agent.api;

import com.resumai.agent.api.dto.CreateTaskRequest;
import com.resumai.agent.api.dto.DashboardMetricsResponse;
import com.resumai.agent.api.dto.TaskResponse;
import com.resumai.agent.service.MvpEvaluationService;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

/**
 * 简历评估任务控制器。
 *
 * <p>提供 MVP 工作台需要的任务创建、任务列表、任务详情和性能指标接口。</p>
 */
@RestController
@RequestMapping("/api")
public class TaskController {

    private final MvpEvaluationService evaluationService;

    public TaskController(MvpEvaluationService evaluationService) {
        this.evaluationService = evaluationService;
    }

    /**
     * 创建简历评估任务。
     *
     * @param request 创建请求
     * @return 任务响应
     */
    @PostMapping("/tasks")
    public TaskResponse createTask(@Valid @RequestBody CreateTaskRequest request) {
        return evaluationService.createTask(request);
    }

    /**
     * 上传简历文件并创建评估任务。
     *
     * <p>公网 MVP 支持 PDF、TXT、Markdown 和 CSV 文件。服务端会抽取正文后复用
     * 原有 Agent 编排链路，避免 HR 先手工复制 PDF 文本。</p>
     *
     * @param file 简历文件
     * @param jobCategory 岗位类别
     * @param executionMode 执行模式
     * @param jobDescription 岗位描述
     * @return 任务响应
     */
    @PostMapping("/tasks/upload")
    public TaskResponse uploadTask(@RequestParam("file") MultipartFile file,
                                   @RequestParam("jobCategory") String jobCategory,
                                   @RequestParam("executionMode") String executionMode,
                                   @RequestParam("jobDescription") String jobDescription) {
        return evaluationService.createTaskFromUpload(file, jobCategory, executionMode, jobDescription);
    }

    /**
     * 查询任务列表。
     *
     * @return 任务列表
     */
    @GetMapping("/tasks")
    public List<TaskResponse> listTasks() {
        return evaluationService.listTasks();
    }

    /**
     * 查询任务详情。
     *
     * @param traceId TraceId
     * @return 任务详情
     */
    @GetMapping("/tasks/{traceId}")
    public TaskResponse getTask(@PathVariable String traceId) {
        return evaluationService.getTask(traceId);
    }

    /**
     * 查询大盘指标。
     *
     * @return 性能指标
     */
    @GetMapping("/metrics")
    public DashboardMetricsResponse metrics() {
        return evaluationService.metrics();
    }
}
