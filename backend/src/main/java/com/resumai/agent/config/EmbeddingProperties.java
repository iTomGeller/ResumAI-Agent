package com.resumai.agent.config;

import java.util.Locale;
import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "resumai.embedding")
public class EmbeddingProperties {

    /** local | openai | openrouter | bailian | zhipu | none */
    private String provider = "local";
    /** 是否启用向量嵌入；local provider 默认 true。 */
    private boolean enabled = true;
    private String baseUrl = "https://api.openai.com/v1";
    private String apiKey = "";
    private String model = "text-embedding-3-small";
    private int readTimeoutMs = 60000;

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getApiKey() {
        return apiKey;
    }

    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    public String getModel() {
        return model;
    }

    public void setModel(String model) {
        this.model = model;
    }

    public int getReadTimeoutMs() {
        return readTimeoutMs;
    }

    public void setReadTimeoutMs(int readTimeoutMs) {
        this.readTimeoutMs = readTimeoutMs;
    }

    public String getProvider() {
        return provider;
    }

    public void setProvider(String provider) {
        this.provider = provider;
    }

    public int resolveDimension() {
        return switch (provider == null ? "local" : provider.toLowerCase(Locale.ROOT)) {
            case "openai", "openrouter" -> 1536;
            case "bailian", "zhipu" -> 1024;
            case "local" -> 384;
            default -> 384;
        };
    }

    public String resolveJdCollectionSuffix() {
        return switch (provider == null ? "local" : provider.toLowerCase(Locale.ROOT)) {
            case "openai" -> "openai_1536";
            case "openrouter" -> "openrouter_1536";
            case "bailian" -> "bailian_1024";
            case "zhipu" -> "zhipu_1024";
            default -> "local_384";
        };
    }

    public boolean isOperational() {
        if (!enabled) {
            return false;
        }
        String p = provider == null ? "local" : provider.toLowerCase(Locale.ROOT);
        if ("local".equals(p)) {
            return true;
        }
        if ("none".equals(p)) {
            return false;
        }
        return apiKey != null && !apiKey.isBlank();
    }
}
