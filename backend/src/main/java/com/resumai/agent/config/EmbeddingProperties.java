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
    /** Default/long-term-memory dimension. Business RAG stages override it. */
    private int dimension;
    /** Joint-search winner dimensions for the three isolated RAG stages. */
    private int resumeDimension = 1024;
    private int jdDimension = 768;
    private int kbDimension = 768;
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

    public int getDimension() {
        return dimension;
    }

    public void setDimension(int dimension) {
        this.dimension = dimension;
    }

    public int getResumeDimension() {
        return resumeDimension;
    }

    public void setResumeDimension(int resumeDimension) {
        this.resumeDimension = resumeDimension;
    }

    public int getJdDimension() {
        return jdDimension;
    }

    public void setJdDimension(int jdDimension) {
        this.jdDimension = jdDimension;
    }

    public int getKbDimension() {
        return kbDimension;
    }

    public void setKbDimension(int kbDimension) {
        this.kbDimension = kbDimension;
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
        if (dimension > 0) {
            return dimension;
        }
        Integer fromModel = dimensionFromModel();
        if (fromModel != null) {
            return fromModel;
        }
        return switch (provider == null ? "local" : provider.toLowerCase(Locale.ROOT)) {
            case "openai", "openrouter" -> 1536;
            case "bailian", "zhipu" -> 1024;
            case "local" -> 384;
            default -> 384;
        };
    }

    public String resolveJdCollectionSuffix() {
        return resolveCollectionSuffix(resolveDimension());
    }

    public int resolveResumeDimension() {
        if ("local".equalsIgnoreCase(provider)) {
            return resolveDimension();
        }
        return resumeDimension > 0 ? resumeDimension : resolveDimension();
    }

    public int resolveJdDimension() {
        if ("local".equalsIgnoreCase(provider)) {
            return resolveDimension();
        }
        return jdDimension > 0 ? jdDimension : resolveDimension();
    }

    public int resolveKbDimension() {
        if ("local".equalsIgnoreCase(provider)) {
            return resolveDimension();
        }
        return kbDimension > 0 ? kbDimension : resolveDimension();
    }

    public String resolveCollectionSuffix(int dim) {
        String p = provider == null ? "local" : provider.toLowerCase(Locale.ROOT);
        // Model-aware suffix so switching OpenRouter Qwen ↔ OpenAI does not collide collections.
        String modelTag = modelCollectionTag();
        return switch (p) {
            case "openai" -> "openai_" + dim;
            case "openrouter" -> "openrouter_" + modelTag + "_" + dim;
            case "bailian" -> "bailian_te3_" + dim;
            case "zhipu" -> "zhipu_" + dim;
            default -> "local_" + dim;
        };
    }

    /** OpenRouter hosts Chinese Qwen3 embeddings with non-1536 dims. */
    private Integer dimensionFromModel() {
        String m = model == null ? "" : model.toLowerCase(Locale.ROOT);
        if (m.contains("qwen3-embedding-8b")) {
            return 4096;
        }
        if (m.contains("qwen3-embedding-4b")) {
            return 2560;
        }
        if (m.contains("qwen3-embedding-0.6") || m.contains("qwen3-embedding-06")) {
            return 1024;
        }
        if (m.contains("text-embedding-3-large")) {
            return 3072;
        }
        if (m.contains("text-embedding-3-small") || m.contains("text-embedding-ada")) {
            return 1536;
        }
        if (m.contains("text-embedding-v3") || m.contains("embedding-3")) {
            return 1024;
        }
        return null;
    }

    private String modelCollectionTag() {
        String m = model == null ? "" : model.toLowerCase(Locale.ROOT);
        if (m.contains("qwen3-embedding-8b")) {
            return "qwen8b";
        }
        if (m.contains("qwen3-embedding-4b")) {
            return "qwen4b";
        }
        if (m.contains("qwen3-embedding-0.6") || m.contains("qwen3-embedding-06")) {
            return "qwen06b";
        }
        if (m.contains("text-embedding-3-small")) {
            return "te3s";
        }
        return "default";
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
