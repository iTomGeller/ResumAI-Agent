package com.resumai.agent.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.WorkflowProperties;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
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
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("traceId", traceId);
        body.put("resumeText", resumeText != null ? resumeText : "");
        body.put("jobCategory", jobCategory);
        body.put("jobDescription", jobDescription);
        body.put("executionMode", executionMode);
        String json = objectMapper.writeValueAsString(body);
        String baseUrl = workflowProperties.getBaseUrl();
        if (!StringUtils.hasText(baseUrl)) {
            throw new IllegalStateException("workflow base-url not configured");
        }
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl.replaceAll("/$", "") + "/workflow/runs"))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json; charset=UTF-8")
                .header("Accept", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() >= 400) {
            throw new IllegalStateException("workflow start failed: HTTP " + response.statusCode() + " " + response.body());
        }
        log.info("workflow started trace={} response={}", traceId, response.body());
    }
}
