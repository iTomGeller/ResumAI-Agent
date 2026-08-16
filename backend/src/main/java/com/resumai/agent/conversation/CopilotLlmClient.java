package com.resumai.agent.conversation;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.DeepSeekProperties;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Consumer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/** Java-owned Copilot model client. It never calls the Python workflow. */
@Component
public class CopilotLlmClient {

    private static final Logger log = LoggerFactory.getLogger(CopilotLlmClient.class);

    private static final String SYSTEM_PROMPT = """
            你是 ResumAI 招聘决策 Copilot。只输出 JSON（不要 markdown）：
            {"answer":"针对当前问题的短答","citations":[],"actions":[],"suggestions":["可选下一步"],"conversationSummary":null}
            规则：
            1. 不要复述完整评估报告；完整报告只在决策报告页。
            2. 不要编造候选人事实、分数或风险；证据不足时明确说明缺什么。
            3. citations 仅在有真实依据时填写；公网技术文档不是候选人履历证据。
            4. 回答控制在 300 个中文字符左右；普通问答不得触发完整评估运行。
            5. contextSnapshot.messagesToCompact 非空时，把既有 conversationSummary 与这些旧消息合并为新的 conversationSummary，最多800个中文字符；否则输出 null。
            6. 有实时工具时由你根据工具描述自主决定是否调用，禁止预设工具名；工具失败时明确说明未查到。
            """;

    private final DeepSeekProperties properties;
    private final CopilotMcpClient mcpClient;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;

    public CopilotLlmClient(DeepSeekProperties properties,
                            CopilotMcpClient mcpClient,
                            ObjectMapper objectMapper) {
        this.properties = properties;
        this.mcpClient = mcpClient;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(
                        Math.max(1000, properties.getConnectTimeoutMs())))
                .build();
    }

    public Optional<Map<String, Object>> reply(Map<String, Object> input,
                                                Consumer<String> onDelta) {
        if (!StringUtils.hasText(properties.getApiKey())
                || !StringUtils.hasText(properties.getApiUrl())) {
            return Optional.empty();
        }
        Consumer<String> sink = onDelta != null ? onDelta : ignored -> { };
        try {
            List<Map<String, Object>> messages = new ArrayList<>();
            messages.add(Map.of("role", "system", "content", SYSTEM_PROMPT));
            messages.add(Map.of("role", "user", "content",
                    objectMapper.writeValueAsString(input)));

            boolean allowTools = Boolean.TRUE.equals(input.get("allowTools"))
                    || "BACKGROUND_QUERY".equalsIgnoreCase(
                            String.valueOf(input.get("disposition")));
            List<Map<String, Object>> tools = allowTools
                    ? mcpClient.providerTools() : List.of();
            List<Map<String, Object>> evidence = new ArrayList<>();

            if (!tools.isEmpty()) {
                Map<String, Object> firstBody = requestBody(messages, false);
                firstBody.put("tools", tools);
                firstBody.put("tool_choice", "BACKGROUND_QUERY".equalsIgnoreCase(
                        String.valueOf(input.get("disposition"))) ? "required" : "auto");
                // Several OpenAI-compatible providers reject response_format
                // while native tools are present.
                firstBody.remove("response_format");
                Map<String, Object> assistant = complete(firstBody);
                List<Map<String, Object>> toolCalls = mapList(assistant.get("tool_calls"));
                if (!toolCalls.isEmpty()) {
                    messages.add(assistantMessage(assistant, toolCalls));
                    Map<String, Object> call = toolCalls.getFirst();
                    Map<String, Object> function = mapValue(call.get("function"));
                    String name = String.valueOf(function.getOrDefault("name", ""));
                    Map<String, Object> arguments = parseArguments(function.get("arguments"));
                    Map<String, Object> result = mcpClient.call(name, arguments);
                    evidence.add(result);
                    messages.add(Map.of(
                            "role", "tool",
                            "tool_call_id", String.valueOf(call.getOrDefault("id", "")),
                            "name", name,
                            "content", clip(objectMapper.writeValueAsString(result), 8000)));
                } else {
                    Map<String, Object> payload = parsePayload(
                            String.valueOf(assistant.getOrDefault("content", "")), evidence);
                    emitWholeAnswer(payload, sink);
                    return Optional.of(payload);
                }
            }

            Map<String, Object> finalBody = requestBody(messages, true);
            Map<String, Object> payload = stream(finalBody, sink, evidence);
            return Optional.of(payload);
        } catch (Exception e) {
            log.info("Java Copilot model call unavailable: {}", e.getMessage());
            return Optional.empty();
        }
    }

    private Map<String, Object> requestBody(List<Map<String, Object>> messages,
                                            boolean stream) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", StringUtils.hasText(properties.getModel())
                ? properties.getModel() : "deepseek-chat");
        body.put("messages", messages);
        body.put("temperature", 0.2);
        body.put("max_tokens", 1000);
        body.put("response_format", Map.of("type", "json_object"));
        // DeepSeek thinking mode rejects required native tool selection and
        // is unnecessary for this short JSON Copilot path. Keep this aligned
        // with workflow/app/runtime/llm.py's provider compatibility rule.
        body.put("thinking", Map.of("type", "disabled"));
        body.put("stream", stream);
        if (stream) {
            body.put("stream_options", Map.of("include_usage", true));
        }
        return body;
    }

    private Map<String, Object> complete(Map<String, Object> body)
            throws IOException, InterruptedException {
        HttpResponse<String> response = httpClient.send(request(body),
                HttpResponse.BodyHandlers.ofString());
        ensureSuccess(response.statusCode(), response.body());
        Map<String, Object> envelope = objectMapper.readValue(response.body(), Map.class);
        List<Map<String, Object>> choices = mapList(envelope.get("choices"));
        if (choices.isEmpty()) {
            throw new IOException("Copilot provider returned no choices");
        }
        return mapValue(choices.getFirst().get("message"));
    }

    private Map<String, Object> stream(Map<String, Object> body,
                                       Consumer<String> sink,
                                       List<Map<String, Object>> evidence)
            throws IOException, InterruptedException {
        HttpResponse<InputStream> response = httpClient.send(request(body),
                HttpResponse.BodyHandlers.ofInputStream());
        if (response.statusCode() >= 400) {
            String error = new String(response.body().readAllBytes(), StandardCharsets.UTF_8);
            ensureSuccess(response.statusCode(), error);
        }

        StringBuilder raw = new StringBuilder();
        String emitted = "";
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                response.body(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (!line.startsWith("data:")) {
                    continue;
                }
                String data = line.substring(5).stripLeading();
                if ("[DONE]".equals(data)) {
                    break;
                }
                if (!StringUtils.hasText(data)) {
                    continue;
                }
                Map<String, Object> chunk = objectMapper.readValue(data, Map.class);
                List<Map<String, Object>> choices = mapList(chunk.get("choices"));
                if (choices.isEmpty()) {
                    continue;
                }
                Map<String, Object> delta = mapValue(choices.getFirst().get("delta"));
                Object content = delta.get("content");
                if (content == null) {
                    continue;
                }
                raw.append(content);
                String partial = extractPartialAnswer(raw.toString());
                if (partial.length() > emitted.length()) {
                    String addition = partial.substring(emitted.length());
                    sink.accept(addition);
                    emitted = partial;
                }
            }
        }
        Map<String, Object> payload = parsePayload(raw.toString(), evidence);
        String answer = String.valueOf(payload.getOrDefault("answer", ""));
        if (answer.length() > emitted.length()) {
            sink.accept(answer.substring(emitted.length()));
        }
        return payload;
    }

    private HttpRequest request(Map<String, Object> body) throws IOException {
        return HttpRequest.newBuilder(chatCompletionsUri(properties.getApiUrl()))
                .timeout(Duration.ofMillis(Math.max(5000, properties.getReadTimeoutMs())))
                .header("Authorization", "Bearer " + properties.getApiKey())
                .header("Content-Type", "application/json; charset=UTF-8")
                .header("Accept", Boolean.TRUE.equals(body.get("stream"))
                        ? "text/event-stream" : "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        objectMapper.writeValueAsString(body)))
                .build();
    }

    /**
     * Deployments historically configure DeepSeek with the OpenAI-compatible
     * API base URL, while application.yml also accepts a complete endpoint.
     * Keep both forms valid so the standalone Copilot behaves like the
     * LangChain4j client it replaced.
     */
    private URI chatCompletionsUri(String configuredUrl) {
        String url = configuredUrl == null ? "" : configuredUrl.trim();
        if (url.isEmpty()) {
            throw new IllegalStateException("DeepSeek API URL is not configured");
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        if (!url.endsWith("/chat/completions")) {
            url += "/chat/completions";
        }
        return URI.create(url);
    }

    private Map<String, Object> parsePayload(String raw,
                                             List<Map<String, Object>> evidence)
            throws IOException {
        String text = raw == null ? "" : raw.trim();
        int start = text.indexOf('{');
        int end = text.lastIndexOf('}');
        Map<String, Object> payload;
        if (start >= 0 && end > start) {
            payload = objectMapper.readValue(text.substring(start, end + 1), Map.class);
        } else {
            payload = new LinkedHashMap<>();
            payload.put("answer", clip(text, 2000));
        }
        if (!StringUtils.hasText(String.valueOf(payload.getOrDefault("answer", "")))) {
            throw new IOException("Copilot provider returned an empty answer");
        }
        Object citations = payload.get("citations");
        if (!(citations instanceof List<?> list) || list.isEmpty()) {
            Map<String, Object> successful = evidence.stream()
                    .filter(item -> Boolean.TRUE.equals(item.get("success")))
                    .findFirst().orElse(null);
            if (successful != null) {
                payload.put("citations", List.of(Map.of(
                        "sourceType", "EXTERNAL",
                        "sourceId", String.valueOf(successful.getOrDefault("tool", "mcp")),
                        "quote", clip(String.valueOf(
                                successful.getOrDefault("text", "")), 180))));
            }
        }
        payload.putIfAbsent("citations", List.of());
        payload.putIfAbsent("actions", List.of());
        payload.putIfAbsent("suggestions", List.of());
        return payload;
    }

    private void emitWholeAnswer(Map<String, Object> payload, Consumer<String> sink) {
        String answer = String.valueOf(payload.getOrDefault("answer", ""));
        if (StringUtils.hasText(answer)) {
            sink.accept(answer);
        }
    }

    /** Extract a stable decoded prefix of the first JSON answer string. */
    private String extractPartialAnswer(String raw) {
        int key = raw.indexOf("\"answer\"");
        if (key < 0) {
            return "";
        }
        int colon = raw.indexOf(':', key + 8);
        int quote = colon < 0 ? -1 : raw.indexOf('"', colon + 1);
        if (quote < 0) {
            return "";
        }
        StringBuilder encoded = new StringBuilder();
        boolean escaped = false;
        for (int i = quote + 1; i < raw.length(); i++) {
            char value = raw.charAt(i);
            if (value == '"' && !escaped) {
                break;
            }
            encoded.append(value);
            if (value == '\\' && !escaped) {
                escaped = true;
            } else {
                escaped = false;
            }
        }
        String safe = trimIncompleteEscape(encoded.toString());
        try {
            return objectMapper.readValue("\"" + safe + "\"", String.class);
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String trimIncompleteEscape(String value) {
        if (value.endsWith("\\")) {
            return value.substring(0, value.length() - 1);
        }
        int marker = value.lastIndexOf("\\u");
        if (marker >= 0 && value.length() - marker < 6) {
            String suffix = value.substring(marker + 2);
            if (suffix.chars().allMatch(ch -> Character.digit(ch, 16) >= 0)) {
                return value.substring(0, marker);
            }
        }
        return value;
    }

    private Map<String, Object> assistantMessage(Map<String, Object> assistant,
                                                  List<Map<String, Object>> calls) {
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("role", "assistant");
        message.put("content", String.valueOf(assistant.getOrDefault("content", "")));
        message.put("tool_calls", calls);
        return message;
    }

    private Map<String, Object> parseArguments(Object raw) {
        if (raw instanceof Map<?, ?> map) {
            return castMap(map);
        }
        try {
            return objectMapper.readValue(String.valueOf(raw), Map.class);
        } catch (Exception ignored) {
            return Map.of();
        }
    }

    private static void ensureSuccess(int statusCode, String body) throws IOException {
        if (statusCode >= 400) {
            throw new IOException("HTTP " + statusCode + ": " + clip(body, 500));
        }
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> mapList(Object raw) {
        if (!(raw instanceof List<?> list)) {
            return List.of();
        }
        List<Map<String, Object>> result = new ArrayList<>();
        for (Object item : list) {
            if (item instanceof Map<?, ?> map) {
                result.add((Map<String, Object>) map);
            }
        }
        return result;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> mapValue(Object raw) {
        return raw instanceof Map<?, ?> map ? (Map<String, Object>) map : Map.of();
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> castMap(Map<?, ?> raw) {
        return (Map<String, Object>) raw;
    }

    private static String clip(String value, int limit) {
        String text = value == null ? "" : value;
        return text.length() <= limit ? text : text.substring(0, limit);
    }
}
