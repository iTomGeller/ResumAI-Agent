package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * DeepSeek 调用配置。
 *
 * <p>所有敏感信息必须来自环境变量或部署密钥，禁止写入代码仓库。
 * 该配置兼容 DeepSeek OpenAI 风格的 Chat Completions API。</p>
 */
@ConfigurationProperties(prefix = "resumai.deepseek")
public class DeepSeekProperties {

    private String apiKey;
    private String apiUrl;
    private String model;
    private int connectTimeoutMs;
    private int readTimeoutMs;

    public String getApiKey() {
        return apiKey;
    }

    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    public String getApiUrl() {
        return apiUrl;
    }

    public void setApiUrl(String apiUrl) {
        this.apiUrl = apiUrl;
    }

    public String getModel() {
        return model;
    }

    public void setModel(String model) {
        this.model = model;
    }

    public int getConnectTimeoutMs() {
        return connectTimeoutMs;
    }

    public void setConnectTimeoutMs(int connectTimeoutMs) {
        this.connectTimeoutMs = connectTimeoutMs;
    }

    public int getReadTimeoutMs() {
        return readTimeoutMs;
    }

    public void setReadTimeoutMs(int readTimeoutMs) {
        this.readTimeoutMs = readTimeoutMs;
    }
}
