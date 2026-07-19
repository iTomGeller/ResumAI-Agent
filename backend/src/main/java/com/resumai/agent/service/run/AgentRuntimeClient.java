package com.resumai.agent.service.run;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.WorkflowProperties;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ThreadLocalRandom;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * HTTP client for the Python agent runtime (/agent/runs). Start requests are
 * idempotent by runId, so transient transport failures are retried (max 2,
 * exponential backoff with jitter). Cancellation propagates a reason and
 * never retries indefinitely — Java remains the authoritative state owner.
 */
@Service
public class AgentRuntimeClient {

    private static final Logger log = LoggerFactory.getLogger(AgentRuntimeClient.class);

    private final WorkflowProperties workflowProperties;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;

    public AgentRuntimeClient(WorkflowProperties workflowProperties, ObjectMapper objectMapper) {
        this.workflowProperties = workflowProperties;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(8))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    public void startRun(Map<String, Object> payload) throws Exception {
        postWithRetry("/agent/runs", payload, Duration.ofSeconds(20), 2);
    }

    /** Resume a paused run: same runId, payload carries resumeSnapshot. */
    public void resumeRun(String runId, Map<String, Object> payload) throws Exception {
        postWithRetry("/agent/runs/" + runId + "/resume", payload,
                Duration.ofSeconds(20), 2);
    }

    public void pauseRun(String runId, String reason) throws Exception {
        postWithRetry("/agent/runs/" + runId + "/pause",
                Map.of("reason", reason != null ? reason : "user_paused"),
                Duration.ofSeconds(10), 1);
    }

    public void cancelRun(String runId, String reason) {
        try {
            postWithRetry("/agent/runs/" + runId + "/cancel",
                    Map.of("reason", reason != null ? reason : "user_cancelled"),
                    Duration.ofSeconds(10), 1);
        } catch (Exception e) {
            log.info("runtime cancel deferred run={}: {}", runId, e.getMessage());
        }
    }

    public Optional<Map<String, Object>> getRun(String runId) {
        try {
            HttpRequest request = builder("/agent/runs/" + runId)
                    .timeout(Duration.ofSeconds(8))
                    .GET()
                    .build();
            HttpResponse<String> response =
                    httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 404) {
                return Optional.empty();
            }
            if (response.statusCode() >= 400) {
                throw new IllegalStateException("HTTP " + response.statusCode());
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> body = objectMapper.readValue(response.body(), Map.class);
            return Optional.of(body);
        } catch (Exception e) {
            log.debug("runtime status query failed run={}: {}", runId, e.getMessage());
            return Optional.empty();
        }
    }

    /** Conversation-turn intent resolution (rule-first, model fallback). */
    @SuppressWarnings("unchecked")
    public Optional<Map<String, Object>> resolveConversationTurn(Map<String, ?> body) {
        try {
            String json = objectMapper.writeValueAsString(body);
            HttpRequest request = builder("/conversation/turns/resolve")
                    .timeout(Duration.ofSeconds(30))
                    .header("Content-Type", "application/json; charset=UTF-8")
                    .POST(HttpRequest.BodyPublishers.ofString(json))
                    .build();
            HttpResponse<String> response =
                    httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() >= 400) {
                throw new IllegalStateException("HTTP " + response.statusCode());
            }
            return Optional.of(objectMapper.readValue(response.body(), Map.class));
        } catch (Exception e) {
            log.info("conversation runtime unavailable, using deterministic router: {}",
                    e.getMessage());
            return Optional.empty();
        }
    }

    private void postWithRetry(String path, Map<String, ?> body, Duration timeout, int maxRetries)
            throws Exception {
        Exception last = null;
        long delayMs = 800;
        for (int attempt = 0; attempt <= maxRetries; attempt++) {
            try {
                String json = objectMapper.writeValueAsString(body);
                HttpRequest request = builder(path)
                        .timeout(timeout)
                        .header("Content-Type", "application/json; charset=UTF-8")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .build();
                HttpResponse<String> response =
                        httpClient.send(request, HttpResponse.BodyHandlers.ofString());
                if (response.statusCode() < 400) {
                    return;
                }
                boolean retryable = response.statusCode() == 429 || response.statusCode() >= 500;
                IllegalStateException failure = new IllegalStateException(
                        "runtime " + path + " failed: HTTP " + response.statusCode()
                                + " " + trim(response.body()));
                if (!retryable) {
                    throw failure;
                }
                last = failure;
            } catch (IllegalStateException nonRetryable) {
                throw nonRetryable;
            } catch (Exception transport) {
                last = transport;
            }
            if (attempt < maxRetries) {
                Thread.sleep(delayMs + ThreadLocalRandom.current().nextLong(200));
                delayMs = Math.min(delayMs * 2, 8000);
            }
        }
        throw last != null ? last : new IllegalStateException("runtime call failed: " + path);
    }

    private HttpRequest.Builder builder(String path) {
        String baseUrl = workflowProperties.getBaseUrl();
        if (!StringUtils.hasText(baseUrl)) {
            throw new IllegalStateException("workflow base-url not configured");
        }
        HttpRequest.Builder request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl.replaceAll("/$", "") + path))
                .header("Accept", "application/json");
        if (StringUtils.hasText(workflowProperties.getInternalToken())) {
            request.header("X-Internal-Token", workflowProperties.getInternalToken());
        }
        return request;
    }

    private String trim(String body) {
        if (body == null) {
            return "";
        }
        return body.length() > 300 ? body.substring(0, 300) : body;
    }
}
