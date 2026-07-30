package com.resumai.agent.api;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.resumai.agent.api.dto.ConversationSnapshotResponse;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.api.dto.ConversationTurnResponse;
import com.resumai.agent.api.dto.TaskControlRequest;
import com.resumai.agent.api.dto.TaskControlResponse;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.service.ConversationService;
import com.resumai.agent.service.TaskControlService;
import com.resumai.agent.util.HrContext;
import jakarta.validation.Valid;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.springframework.util.StringUtils;
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
public class ConversationController {

    private final ConversationService conversationService;
    private final TaskControlService taskControlService;
    private final AgentRunMapper agentRunMapper;

    public ConversationController(ConversationService conversationService,
                                  TaskControlService taskControlService,
                                  AgentRunMapper agentRunMapper) {
        this.conversationService = conversationService;
        this.taskControlService = taskControlService;
        this.agentRunMapper = agentRunMapper;
    }

    public record CreateConversationBody(String title, String resumeText, String jobDescription,
                                         String jobCategory, String fromTraceId) {
    }

    @PostMapping("/conversations")
    public Map<String, Object> createConversation(@RequestBody CreateConversationBody body) {
        ConversationSession session = conversationService.createConversation(
                new ConversationService.CreateConversationRequest(
                        body.title(), body.resumeText(), body.jobDescription(),
                        body.jobCategory(), body.fromTraceId()));
        return sessionView(session);
    }

    @PostMapping("/conversations/upload")
    public Map<String, Object> createConversationFromUpload(
            @RequestParam("file") MultipartFile file,
            @RequestParam(value = "title", required = false) String title,
            @RequestParam(value = "jobDescription", required = false) String jobDescription,
            @RequestParam(value = "jobCategory", required = false) String jobCategory) {
        String resumeText = extractText(file);
        ConversationSession session = conversationService.createConversation(
                new ConversationService.CreateConversationRequest(
                        title, resumeText, jobDescription, jobCategory, null));
        return sessionView(session);
    }

    @GetMapping("/conversations")
    public List<Map<String, Object>> listConversations(
            @RequestParam(value = "limit", defaultValue = "50") int limit) {
        return conversationService.listConversations(HrContext.getHrId(), limit).stream()
                .map(this::sessionView)
                .toList();
    }

    @GetMapping("/conversations/{conversationId}")
    public ConversationSnapshotResponse getConversation(@PathVariable String conversationId) {
        return conversationService.getSnapshot(conversationId);
    }

    @GetMapping("/conversations/{conversationId}/runs")
    public List<Map<String, Object>> listRuns(@PathVariable String conversationId,
                                              @RequestParam(value = "limit", defaultValue = "50")
                                              int limit) {
        List<AgentRun> runs = agentRunMapper.selectList(new QueryWrapper<AgentRun>()
                .eq("conversation_id", conversationId)
                .orderByDesc("created_at")
                .last("limit " + Math.min(Math.max(limit, 1), 200)));
        return runs.stream().map(this::runView).toList();
    }

    public record AttachContextBody(String resumeText, String jobDescription, String jobCategory) {
    }

    @PostMapping("/conversations/{conversationId}/context")
    public Map<String, Object> attachContext(@PathVariable String conversationId,
                                             @RequestBody AttachContextBody body) {
        conversationService.attachContext(conversationId, body.resumeText(),
                body.jobDescription(), body.jobCategory());
        return sessionView(conversationService.getSession(conversationId));
    }

    @PostMapping("/conversations/{conversationId}/messages")
    public ConversationTurnResponse sendMessage(
            @PathVariable String conversationId,
            @Valid @RequestBody ConversationTurnRequest request) {
        return conversationService.sendTurn(conversationId, request);
    }

    public record IntentPreviewBody(String content) {
    }

    /**
     * EXP-6 evaluation hook: rule-layer classification only, no run is created
     * and no LLM is called. UNCLASSIFIED marks the LLM-second-pass boundary.
     */
    @PostMapping("/conversations/intent-preview")
    public Map<String, Object> previewIntent(@RequestBody IntentPreviewBody body) {
        var decision = conversationService.classifyRuleOnly(body.content());
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("intent", decision.intent());
        view.put("action", decision.action());
        view.put("affectsEvaluation", decision.affectsEvaluation());
        view.put("needsConfirmation", decision.needsConfirmation());
        view.put("llmSecondPass", "UNCLASSIFIED".equals(decision.intent()));
        return view;
    }

    @PostMapping("/tasks/{traceId}/control")
    public TaskControlResponse controlTask(
            @PathVariable String traceId,
            @Valid @RequestBody TaskControlRequest request) {
        return taskControlService.control(traceId, request.action(), request.approvedPlan());
    }

    private Map<String, Object> sessionView(ConversationSession session) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("conversationId", session.getId());
        view.put("userId", session.getUserId());
        view.put("title", session.getTitle());
        view.put("jobCategory", session.getJobCategory());
        view.put("hasResume", StringUtils.hasText(session.getResumeText()));
        view.put("hasJobDescription", StringUtils.hasText(session.getJobDescription()));
        view.put("resumeChars", session.getResumeText() != null ? session.getResumeText().length() : 0);
        view.put("summary", session.getSummary());
        view.put("currentGoal", session.getCurrentGoal());
        view.put("activeTraceId", session.getActiveTraceId());
        view.put("activeRevision", session.getActiveRevision());
        view.put("createdAt", String.valueOf(session.getCreateTime()));
        view.put("updatedAt", String.valueOf(session.getUpdateTime()));
        return view;
    }

    private Map<String, Object> runView(AgentRun run) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("runId", run.getRunId());
        view.put("status", run.getStatus());
        view.put("runType", run.getRunType());
        view.put("queueMode", run.getQueueMode());
        view.put("currentAgent", run.getCurrentAgent());
        view.put("currentTool", run.getCurrentTool());
        view.put("errorCode", run.getErrorCode());
        view.put("createdAt", String.valueOf(run.getCreatedAt()));
        view.put("finishedAt", String.valueOf(run.getFinishedAt()));
        return view;
    }

    private String extractText(MultipartFile file) {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("请上传 PDF/TXT/Markdown 简历文件");
        }
        if (file.getSize() > 20L * 1024 * 1024) {
            throw new IllegalArgumentException("简历文件不能超过 20MB");
        }
        String name = file.getOriginalFilename() != null
                ? file.getOriginalFilename().toLowerCase() : "resume";
        try {
            String text;
            if (name.endsWith(".pdf")) {
                try (PDDocument document = Loader.loadPDF(file.getBytes())) {
                    text = new PDFTextStripper().getText(document);
                }
            } else if (name.endsWith(".txt") || name.endsWith(".md") || name.endsWith(".csv")) {
                text = new String(file.getBytes(), StandardCharsets.UTF_8);
            } else {
                throw new IllegalArgumentException("暂不支持该文件类型，请上传 PDF/TXT/Markdown/CSV");
            }
            String normalized = text == null ? "" : text.replace('\u0000', ' ').trim();
            if (!StringUtils.hasText(normalized)) {
                throw new IllegalArgumentException("未能从简历文件中提取有效文本（可能是扫描件）");
            }
            return normalized;
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalArgumentException("简历解析失败：" + e.getMessage());
        }
    }
}
