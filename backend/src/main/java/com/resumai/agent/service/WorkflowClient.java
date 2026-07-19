package com.resumai.agent.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.WorkflowProperties;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class WorkflowClient {

    private static final Logger log = LoggerFactory.getLogger(WorkflowClient.class);

    private final WorkflowProperties workflowProperties;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;

    public WorkflowClient(WorkflowProperties workflowProperties, ObjectMapper objectMapper) {
        this.workflowProperties = workflowProperties;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    public void startWorkflow(String traceId, String resumeText, String jobCategory,
                              String jobDescription, String executionMode) throws Exception {
        startWorkflow(traceId, traceId, traceId, 1, resumeText, jobCategory,
                jobDescription, executionMode, "", null, null, List.of());
    }

    public void startWorkflow(String traceId,
                              String workflowRunId,
                              String conversationId,
                              Integer revisionNo,
                              String resumeText,
                              String jobCategory,
                              String jobDescription,
                              String executionMode,
                              String evaluationBrief,
                              String baseTraceId,
                              String baseWorkflowRunId,
                              List<String> invalidatedNodes) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("traceId", traceId);
        body.put("workflowRunId", workflowRunId);
        body.put("conversationId", conversationId);
        body.put("revision", revisionNo != null ? revisionNo : 1);
        body.put("resumeText", resumeText != null ? resumeText : "");
        body.put("jobCategory", jobCategory);
        body.put("jobDescription", jobDescription);
        body.put("executionMode", executionMode);
        Map<String, Object> brief = new LinkedHashMap<>();
        if (StringUtils.hasText(evaluationBrief)) {
            brief.put("instruction", evaluationBrief);
            brief.put("source", "conversation_turn");
        }
        body.put("evaluationBrief", brief);
        body.put("baseTraceId", baseTraceId);
        body.put("baseWorkflowRunId", baseWorkflowRunId);
        body.put("affectedNodes", invalidatedNodes != null ? invalidatedNodes : List.of());
        body.put("invalidatedNodes", invalidatedNodes != null ? invalidatedNodes : List.of());
        postJson("/workflow/runs", body, Duration.ofSeconds(10));
        log.info("workflow started trace={} run={} revision={}", traceId, workflowRunId, revisionNo);
    }

    public void controlWorkflow(String workflowRunId, String action) throws Exception {
        controlWorkflow(workflowRunId, action, null, null, null);
    }

    public void controlWorkflow(String workflowRunId,
                                String action,
                                String traceId,
                                String conversationId,
                                Integer revisionNo) throws Exception {
        if (!StringUtils.hasText(workflowRunId)) {
            throw new IllegalArgumentException("workflowRunId is required");
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("action", action);
        if (StringUtils.hasText(traceId)) {
            body.put("traceId", traceId);
        }
        if (StringUtils.hasText(conversationId)) {
            body.put("conversationId", conversationId);
        }
        if (revisionNo != null) {
            body.put("revision", revisionNo);
        }
        postJson(
                "/workflow/runs/" + workflowRunId + "/control",
                body,
                Duration.ofSeconds(10));
    }

    @SuppressWarnings("unchecked")
    public Optional<Map<String, Object>> resolveConversationTurn(Map<String, ?> body) {
        try {
            String json = postJson("/conversation/turns/resolve", body, Duration.ofSeconds(30));
            return Optional.of(objectMapper.readValue(json, Map.class));
        } catch (Exception e) {
            log.info("conversation runtime unavailable, using deterministic router: {}", e.getMessage());
            return Optional.empty();
        }
    }

    private String postJson(String path, Map<String, ?> body, Duration timeout) throws Exception {
        String json = objectMapper.writeValueAsString(body);
        String baseUrl = workflowProperties.getBaseUrl();
        if (!StringUtils.hasText(baseUrl)) {
            throw new IllegalStateException("workflow base-url not configured");
        }
        HttpRequest.Builder requestBuilder = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl.replaceAll("/$", "") + path))
                .timeout(timeout)
                .header("Content-Type", "application/json; charset=UTF-8")
                .header("Accept", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json));
        if (StringUtils.hasText(workflowProperties.getInternalToken())) {
            requestBuilder.header("X-Internal-Token", workflowProperties.getInternalToken());
        }
        HttpRequest request = requestBuilder.build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() >= 400) {
            throw new IllegalStateException("workflow request failed: HTTP " + response.statusCode() + " " + response.body());
        }
        return response.body();
    }
}
