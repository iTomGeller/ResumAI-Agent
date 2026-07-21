package com.resumai.agent.config;

import org.springframework.stereotype.Component;

@Component
public class EmbeddingAvailability {

    public static final String DISABLED_REASON = "RAG_DISABLED_BY_CONFIG";

    private final EmbeddingProperties properties;

    public EmbeddingAvailability(EmbeddingProperties properties) {
        this.properties = properties;
    }

    public boolean isOperational() {
        return properties.isOperational();
    }

    public String disabledReason() {
        if (!properties.isEnabled()) {
            return DISABLED_REASON;
        }
        if (properties.isOperational()) {
            return "";
        }
        String provider = properties.getProvider() == null ? "local" : properties.getProvider().toLowerCase();
        if (!"local".equals(provider)
                && (properties.getApiKey() == null || properties.getApiKey().isBlank())) {
            return "EMBEDDING_API_KEY_MISSING";
        }
        return DISABLED_REASON;
    }

    public String statusMessage() {
        if (isOperational()) {
            String provider = properties.getProvider() == null ? "local" : properties.getProvider();
            if ("local".equalsIgnoreCase(provider)) {
                return "当前使用本地 MiniLM-L6-v2 向量检索（384 维）";
            }
            return "向量检索：" + properties.getModel() + "（" + provider + "，"
                    + properties.resolveDimension() + " 维）";
        }
        return "当前已自动回退到关键词匹配，岗位匹配仍可用";
    }
}
