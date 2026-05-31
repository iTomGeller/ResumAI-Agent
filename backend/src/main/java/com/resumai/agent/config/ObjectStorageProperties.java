package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * MinIO / S3 兼容对象存储配置。
 */
@ConfigurationProperties(prefix = "resumai.object-storage")
public class ObjectStorageProperties {

    /** 是否启用对象存储（false 时回退本地文件系统）。 */
    private boolean enabled = false;
    private String endpoint = "http://minio:9000";
    private String accessKey = "";
    private String secretKey = "";
    private String bucket = "resumai";
    private String region = "us-east-1";

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public String getEndpoint() {
        return endpoint;
    }

    public void setEndpoint(String endpoint) {
        this.endpoint = endpoint;
    }

    public String getAccessKey() {
        return accessKey;
    }

    public void setAccessKey(String accessKey) {
        this.accessKey = accessKey;
    }

    public String getSecretKey() {
        return secretKey;
    }

    public void setSecretKey(String secretKey) {
        this.secretKey = secretKey;
    }

    public String getBucket() {
        return bucket;
    }

    public void setBucket(String bucket) {
        this.bucket = bucket;
    }

    public String getRegion() {
        return region;
    }

    public void setRegion(String region) {
        this.region = region;
    }
}
