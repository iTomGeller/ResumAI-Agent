package com.resumai.agent.config;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * Langfuse exporter enable/disable gate and Ops health snapshot.
 * Exporter starts only when endpoint + public key + secret key are all present.
 */
@Component
public class LangfuseHealthService {

    public enum Status {
        DISABLED,
        AUTH_REQUIRED,
        READY,
        UNREACHABLE,
        AUTH_FAILED
    }

    private static final Logger log = LoggerFactory.getLogger(LangfuseHealthService.class);
    private static final Duration PROBE_TIMEOUT = Duration.ofSeconds(3);

    private final String otelEndpoint;
    private final String publicKey;
    private final String secretKey;
    private final String publicUrl;

    private final AtomicReference<Instant> lastSuccessAt = new AtomicReference<>();
    private final AtomicReference<Instant> lastErrorAt = new AtomicReference<>();
    private final AtomicReference<String> lastError = new AtomicReference<>();
    private final AtomicReference<Status> probeStatus = new AtomicReference<>();
    private final AtomicReference<Instant> lastProbedAt = new AtomicReference<>();

    public LangfuseHealthService(
            @Value("${langfuse.otel-endpoint:}") String otelEndpoint,
            @Value("${langfuse.public-key:}") String publicKey,
            @Value("${langfuse.secret-key:}") String secretKey,
            @Value("${langfuse.public-url:}") String publicUrl) {
        this.otelEndpoint = trim(otelEndpoint);
        this.publicKey = trim(publicKey);
        this.secretKey = trim(secretKey);
        this.publicUrl = trim(publicUrl);
    }

    public static LangfuseHealthService forTest(String endpoint, String publicKey, String secretKey, String publicUrl) {
        return new LangfuseHealthService(endpoint, publicKey, secretKey, publicUrl);
    }

    public boolean isExporterEnabled() {
        return StringUtils.hasText(otelEndpoint)
                && StringUtils.hasText(publicKey)
                && StringUtils.hasText(secretKey);
    }

    public String disableReason() {
        if (isExporterEnabled()) {
            return "";
        }
        boolean hasEndpoint = StringUtils.hasText(otelEndpoint);
        boolean hasKeys = StringUtils.hasText(publicKey) && StringUtils.hasText(secretKey);
        if (hasEndpoint && !hasKeys) {
            return "AUTH_REQUIRED: LANGFUSE_PUBLIC_KEY/SECRET_KEY missing; exporter disabled to avoid 401";
        }
        if (!hasEndpoint && hasKeys) {
            return "DISABLED: LANGFUSE_OTEL_ENDPOINT (or OTEL_EXPORTER_OTLP_ENDPOINT) not set";
        }
        if (!hasEndpoint) {
            return "DISABLED: Langfuse not configured (endpoint + keys required)";
        }
        if (!StringUtils.hasText(publicKey)) {
            return "AUTH_REQUIRED: LANGFUSE_PUBLIC_KEY missing";
        }
        return "AUTH_REQUIRED: LANGFUSE_SECRET_KEY missing";
    }

    public boolean hasValidPublicUrl() {
        if (!StringUtils.hasText(publicUrl)) {
            return false;
        }
        String lower = publicUrl.toLowerCase(Locale.ROOT);
        return lower.startsWith("http://") || lower.startsWith("https://");
    }

    /**
     * External Trace links only when exporter can ingest and a public UI URL is set.
     * Probe AUTH_FAILED / UNREACHABLE blocks dead links.
     */
    public boolean canEmitExternalLinks() {
        if (!isExporterEnabled() || !hasValidPublicUrl()) {
            return false;
        }
        Status s = status();
        return s == Status.READY;
    }

    public Status status() {
        if (!StringUtils.hasText(otelEndpoint)
                && !StringUtils.hasText(publicKey)
                && !StringUtils.hasText(secretKey)) {
            return Status.DISABLED;
        }
        if (!isExporterEnabled()) {
            boolean hasEndpoint = StringUtils.hasText(otelEndpoint);
            if (hasEndpoint) {
                return Status.AUTH_REQUIRED;
            }
            return Status.DISABLED;
        }
        Status probed = probeStatus.get();
        if (probed != null) {
            return probed;
        }
        return Status.READY;
    }

    public String statusReason() {
        Status s = status();
        return switch (s) {
            case DISABLED -> disableReason().isBlank()
                    ? "Langfuse 未配置"
                    : disableReason();
            case AUTH_REQUIRED -> disableReason();
            case AUTH_FAILED -> lastError.get() != null
                    ? lastError.get()
                    : "Langfuse 认证失败：请检查 PUBLIC/SECRET key";
            case UNREACHABLE -> lastError.get() != null
                    ? lastError.get()
                    : "Langfuse 不可达";
            case READY -> hasValidPublicUrl()
                    ? "Langfuse exporter 已启用"
                    : "Langfuse exporter 已启用，但 LANGFUSE_PUBLIC_URL 未配置，无法生成外链";
        };
    }

    public String buildTraceUrl(String traceId) {
        if (!StringUtils.hasText(traceId) || !canEmitExternalLinks()) {
            return "";
        }
        String base = publicUrl.endsWith("/")
                ? publicUrl.substring(0, publicUrl.length() - 1)
                : publicUrl;
        return base + "/project/resumai-project/traces/" + traceId;
    }

    public void recordExportSuccess() {
        lastSuccessAt.set(Instant.now());
        lastError.set(null);
        probeStatus.set(Status.READY);
    }

    public void recordExportError(String message) {
        lastErrorAt.set(Instant.now());
        String msg = message == null ? "export failed" : message;
        lastError.set(msg.length() > 300 ? msg.substring(0, 300) : msg);
        String lower = msg.toLowerCase(Locale.ROOT);
        if (lower.contains("401") || lower.contains("403") || lower.contains("unauthorized")
                || lower.contains("auth")) {
            probeStatus.set(Status.AUTH_FAILED);
        } else {
            probeStatus.set(Status.UNREACHABLE);
        }
    }

    /** Best-effort reachability probe; never throws. */
    public void refreshProbe() {
        if (!isExporterEnabled()) {
            probeStatus.set(null);
            return;
        }
        String healthUrl = resolveHealthUrl();
        if (!StringUtils.hasText(healthUrl)) {
            probeStatus.set(Status.READY);
            return;
        }
        try {
            HttpClient client = HttpClient.newBuilder()
                    .connectTimeout(PROBE_TIMEOUT)
                    .build();
            HttpRequest request = HttpRequest.newBuilder(URI.create(healthUrl))
                    .timeout(PROBE_TIMEOUT)
                    .GET()
                    .build();
            HttpResponse<Void> response = client.send(request, HttpResponse.BodyHandlers.discarding());
            lastProbedAt.set(Instant.now());
            int code = response.statusCode();
            if (code == 401 || code == 403) {
                recordExportError("probe HTTP " + code);
            } else if (code >= 200 && code < 500) {
                // Langfuse health often returns 200; some paths 404 still mean host is up.
                recordExportSuccess();
            } else {
                recordExportError("probe HTTP " + code);
            }
        } catch (Exception e) {
            lastProbedAt.set(Instant.now());
            recordExportError("probe failed: " + e.getMessage());
            log.debug("[langfuse] health probe failed: {}", e.toString());
        }
    }

    public Map<String, Object> snapshot() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("enabled", isExporterEnabled());
        body.put("status", status().name());
        body.put("reason", statusReason());
        body.put("endpoint", redactEndpoint(otelEndpoint));
        body.put("publicUrlConfigured", hasValidPublicUrl());
        body.put("publicUrl", hasValidPublicUrl() ? publicUrl : "");
        body.put("authConfigured", StringUtils.hasText(publicKey) && StringUtils.hasText(secretKey));
        body.put("externalLinksEnabled", canEmitExternalLinks());
        body.put("lastSuccessAt", lastSuccessAt.get() == null ? null : lastSuccessAt.get().toString());
        body.put("lastErrorAt", lastErrorAt.get() == null ? null : lastErrorAt.get().toString());
        body.put("lastError", lastError.get());
        body.put("lastProbedAt", lastProbedAt.get() == null ? null : lastProbedAt.get().toString());
        return body;
    }

    public Map<String, Object> linkMeta(String traceId) {
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("langfuseTraceId", StringUtils.hasText(traceId) ? traceId : "");
        meta.put("langfuseTraceUrl", buildTraceUrl(traceId));
        meta.put("langfuseStatus", status().name());
        meta.put("langfuseStatusReason", statusReason());
        meta.put("langfuseExternalLinksEnabled", StringUtils.hasText(buildTraceUrl(traceId)));
        return meta;
    }

    public String getOtelEndpoint() {
        return otelEndpoint;
    }

    public String getPublicKey() {
        return publicKey;
    }

    public String getSecretKey() {
        return secretKey;
    }

    private String resolveHealthUrl() {
        if (hasValidPublicUrl()) {
            String base = publicUrl.endsWith("/")
                    ? publicUrl.substring(0, publicUrl.length() - 1)
                    : publicUrl;
            return base + "/api/public/health";
        }
        if (!StringUtils.hasText(otelEndpoint)) {
            return "";
        }
        try {
            URI uri = URI.create(otelEndpoint);
            if (uri.getScheme() == null || uri.getHost() == null) {
                return "";
            }
            int port = uri.getPort();
            String authority = port > 0 ? uri.getHost() + ":" + port : uri.getHost();
            return uri.getScheme() + "://" + authority + "/api/public/health";
        } catch (Exception e) {
            return "";
        }
    }

    static String redactEndpoint(String endpoint) {
        if (!StringUtils.hasText(endpoint)) {
            return "";
        }
        try {
            URI uri = URI.create(endpoint);
            if (uri.getHost() == null) {
                return endpoint.length() > 48 ? endpoint.substring(0, 48) + "…" : endpoint;
            }
            int port = uri.getPort();
            String host = uri.getHost();
            String path = uri.getPath() == null ? "" : uri.getPath();
            return uri.getScheme() + "://" + host + (port > 0 ? ":" + port : "") + path;
        } catch (Exception e) {
            return "***";
        }
    }

    private static String trim(String value) {
        return value == null ? "" : value.trim();
    }
}
