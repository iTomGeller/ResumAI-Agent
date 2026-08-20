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
            5. conversationCompressionContext.messagesToCompact 非空时，把既有 conversationSummary 与这些旧消息合并为新的 conversationSummary，最多800个中文字符；否则输出 null。
            6. 有实时工具时由你根据工具描述自主决定是否调用，禁止预设工具名；工具失败时明确说明未查到。
            7. “候选人固定上下文”与“当前会话与问题”共同构成本轮输入；当前问题和近期消息以后者为准。
            """;

    private static final List<String> STABLE_SNAPSHOT_KEYS = List.of(
            "jobCategory", "revision", "hasResume", "hasJobDescription",
            "resumeText", "jobDescription", "structuredReport");

    private final DeepSeekProperties properties;
    private final CopilotMcpClient mcpClient;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;
    private final CopilotMetrics metrics;

    private record PromptParts(
            String systemContextJson,
            List<Map<String, Object>> historyMessages,
            String currentUserContent
    ) {
    }

    public CopilotLlmClient(DeepSeekProperties properties,
                            CopilotMcpClient mcpClient,
                            ObjectMapper objectMapper,
                            CopilotMetrics metrics) {
        this.properties = properties;
        this.mcpClient = mcpClient;
        this.objectMapper = objectMapper;
        this.metrics = metrics;
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
            PromptParts prompt = promptParts(input);
            List<Map<String, Object>> messages = new ArrayList<>();
            messages.add(Map.of("role", "system", "content",
                    SYSTEM_PROMPT + "\n[候选人与会话稳定上下文]\n"
                            + prompt.systemContextJson()));
            messages.addAll(prompt.historyMessages());
            messages.add(Map.of("role", "user", "content", prompt.currentUserContent()));

            boolean allowTools = Boolean.TRUE.equals(input.get("allowTools"))
                    || "BACKGROUND_QUERY".equalsIgnoreCase(
                            String.valueOf(input.get("disposition")));
            List<Map<String, Object>> tools = allowTools
                    ? mcpClient.providerTools() : List.of();
            List<Map<String, Object>> evidence = new ArrayList<>();

            if (!tools.isEmpty()) {
                // A documentation lookup commonly needs discovery followed by
                // retrieval. Allow two generic model-selected ReAct rounds;
                // no server or tool name is encoded in this loop.
                for (int round = 0; round < 2; round++) {
                    Map<String, Object> toolBody = requestBody(messages, false);
                    toolBody.put("tools", tools);
                    toolBody.put("parallel_tool_calls", false);
                    toolBody.put("tool_choice", round == 0
                            && "BACKGROUND_QUERY".equalsIgnoreCase(
                            String.valueOf(input.get("disposition")))
                            ? "required" : "auto");
                    // Several OpenAI-compatible providers reject response_format
                    // while native tools are present.
                    toolBody.remove("response_format");
                    Map<String, Object> assistant = complete(toolBody);
                    List<Map<String, Object>> toolCalls = mapList(assistant.get("tool_calls"));
                    if (toolCalls.isEmpty()) {
                        Map<String, Object> payload = parsePayload(
                                String.valueOf(assistant.getOrDefault("content", "")), evidence);
                        emitWholeAnswer(payload, sink);
                        return Optional.of(payload);
                    }
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
                            "content", toolMessageContent(result)));
                }
            }

            Map<String, Object> finalBody = requestBody(messages, true);
            Map<String, Object> payload = stream(finalBody, sink, evidence);
            return Optional.of(payload);
        } catch (Exception e) {
            metrics.recordProviderFailure();
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
        long providerStartedNanos = System.nanoTime();
        HttpResponse<String> response = httpClient.send(request(body),
                HttpResponse.BodyHandlers.ofString());
        long totalMs = elapsedMs(providerStartedNanos);
        metrics.recordProviderCall(totalMs, totalMs, totalMs);
        ensureSuccess(response.statusCode(), response.body());
        Map<String, Object> envelope = objectMapper.readValue(response.body(), Map.class);
        recordUsage(envelope);
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
        long providerStartedNanos = System.nanoTime();
        HttpResponse<InputStream> response = httpClient.send(request(body),
                HttpResponse.BodyHandlers.ofInputStream());
        long headerLatencyMs = elapsedMs(providerStartedNanos);
        long firstTokenNanos = 0;
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
                recordUsage(chunk);
                List<Map<String, Object>> choices = mapList(chunk.get("choices"));
                if (choices.isEmpty()) {
                    continue;
                }
                Map<String, Object> delta = mapValue(choices.getFirst().get("delta"));
                Object content = delta.get("content");
                if (content == null) {
                    continue;
                }
                if (firstTokenNanos == 0) {
                    firstTokenNanos = System.nanoTime();
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
        long completedNanos = System.nanoTime();
        metrics.recordProviderCall(
                headerLatencyMs,
                firstTokenNanos == 0
                        ? elapsedMs(providerStartedNanos)
                        : elapsedMs(providerStartedNanos, firstTokenNanos),
                elapsedMs(providerStartedNanos, completedNanos));
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

    /**
     * DeepSeek caches only identical prefixes. Candidate material changes only
     * when the conversation revision changes, so serialize it before the
     * current question and recent turns. Dynamic values must never precede it.
     */
    private PromptParts promptParts(Map<String, Object> input) throws IOException {
        Map<String, Object> systemContext = new LinkedHashMap<>();
        Map<String, Object> sourceSnapshot = mapValue(input.get("contextSnapshot"));
        Map<String, Object> stableSnapshot = new LinkedHashMap<>();
        for (String key : STABLE_SNAPSHOT_KEYS) {
            if (sourceSnapshot.containsKey(key)) {
                stableSnapshot.put(key, sourceSnapshot.get(key));
            }
        }
        if (!stableSnapshot.isEmpty()) {
            systemContext.put("candidateContext", stableSnapshot);
        }

        Map<String, Object> compactContext = new LinkedHashMap<>();
        for (String key : List.of("activeGoal", "summary", "conversationSummary",
                "messagesToCompact")) {
            Object value = sourceSnapshot.get(key);
            if (value != null && (!(value instanceof List<?> list) || !list.isEmpty())) {
                compactContext.put(key, value);
            }
        }
        if (!compactContext.isEmpty()) {
            systemContext.put("conversationCompressionContext", compactContext);
        }

        List<Map<String, Object>> history = new ArrayList<>();
        for (Map<String, Object> item : mapList(sourceSnapshot.get("recentMessages"))) {
            String role = String.valueOf(item.getOrDefault("role", "")).toLowerCase();
            String content = String.valueOf(item.getOrDefault("content", ""));
            if (("user".equals(role) || "assistant".equals(role))
                    && StringUtils.hasText(content)) {
                history.add(Map.of(
                        "role", role,
                        "content", "assistant".equals(role)
                                ? historicalAssistantJson(content) : content));
            }
        }

        String currentContent = String.valueOf(input.getOrDefault("content", ""));
        List<Map<String, Object>> contextRefs = mapList(input.get("contextRefs"));
        if (!contextRefs.isEmpty()) {
            currentContent = "[引用上下文]\n"
                    + objectMapper.writeValueAsString(contextRefs)
                    + "\n[当前问题]\n" + currentContent;
        }
        return new PromptParts(
                objectMapper.writeValueAsString(systemContext),
                history,
                currentContent);
    }

    /** Keep historical assistant turns consistent with the active JSON mode. */
    private String historicalAssistantJson(String answer) throws IOException {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("answer", answer);
        payload.put("citations", List.of());
        payload.put("actions", List.of());
        payload.put("suggestions", List.of());
        payload.put("conversationSummary", null);
        return objectMapper.writeValueAsString(payload);
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

    private void recordUsage(Map<String, Object> envelope) {
        Map<String, Object> usage = mapValue(envelope.get("usage"));
        if (usage.isEmpty()) {
            return;
        }
        Map<String, Object> promptDetails = mapValue(
                usage.get("prompt_tokens_details"));
        Integer cachedTokens = integerValue(promptDetails.get("cached_tokens"));
        if (cachedTokens == null) {
            cachedTokens = integerValue(promptDetails.get("prompt_cache_hit_tokens"));
        }
        if (cachedTokens == null) {
            cachedTokens = integerValue(usage.get("prompt_cache_hit_tokens"));
        }
        metrics.recordProviderUsage(
                integerValue(usage.get("prompt_tokens")),
                cachedTokens);
    }

    private static Integer integerValue(Object raw) {
        if (raw instanceof Number number) {
            return number.intValue();
        }
        try {
            return raw == null ? null : Integer.valueOf(String.valueOf(raw));
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    private static long elapsedMs(long startedNanos) {
        return Math.max(0, (System.nanoTime() - startedNanos) / 1_000_000);
    }

    private static long elapsedMs(long startedNanos, long endedNanos) {
        return Math.max(0, (endedNanos - startedNanos) / 1_000_000);
    }

    private static String clip(String value, int limit) {
        String text = value == null ? "" : value;
        return text.length() <= limit ? text : text.substring(0, limit);
    }

    private static String clipPreservingHeadTail(String value, int limit) {
        String text = value == null ? "" : value;
        if (text.length() <= limit) {
            return text;
        }
        String marker = "\n[…中间内容已截断…]\n";
        int available = Math.max(2, limit - marker.length());
        int head = Math.max(1, (int) Math.ceil(available * 0.6));
        int tail = Math.max(1, available - head);
        return text.substring(0, head) + marker
                + text.substring(text.length() - tail);
    }

    /** Keep the tool envelope valid; only large payload fields are clipped. */
    private String toolMessageContent(Map<String, Object> result) throws IOException {
        Map<String, Object> safe = new LinkedHashMap<>();
        for (String key : List.of("success", "status", "tool", "mcpServer")) {
            if (result.containsKey(key)) {
                safe.put(key, result.get(key));
            }
        }
        safe.put("text", clipPreservingHeadTail(
                String.valueOf(result.getOrDefault("text", "")), 6200));
        if (result.containsKey("structuredContent")) {
            safe.put("structuredContent", clipPreservingHeadTail(
                    objectMapper.writeValueAsString(result.get("structuredContent")), 1200));
        }
        return objectMapper.writeValueAsString(safe);
    }
}
