package com.resumai.agent.service;

import com.resumai.agent.config.ObjectStorageProperties;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;

/**
 * MinIO 对象存储服务 — 简历原件与大文本冷存储。
 * 未启用时所有操作返回 null，调用方回退本地文件系统。
 */
@Service
public class ObjectStorageService {

    private static final Logger log = LoggerFactory.getLogger(ObjectStorageService.class);

    private final ObjectStorageProperties properties;
    private volatile S3Client client;

    public ObjectStorageService(ObjectStorageProperties properties) {
        this.properties = properties;
    }

    public boolean isEnabled() {
        return properties.isEnabled()
                && StringUtils.hasText(properties.getEndpoint())
                && StringUtils.hasText(properties.getAccessKey());
    }

    public String putText(String objectKey, String content) {
        if (!isEnabled() || !StringUtils.hasText(objectKey) || content == null) {
            return null;
        }
        try {
            ensureBucket();
            client().putObject(
                    PutObjectRequest.builder()
                            .bucket(properties.getBucket())
                            .key(objectKey)
                            .contentType("text/plain; charset=utf-8")
                            .build(),
                    RequestBody.fromString(content, StandardCharsets.UTF_8));
            return objectKey;
        } catch (Exception e) {
            log.warn("Object storage putText failed (key={}): {}", objectKey, e.getMessage());
            return null;
        }
    }

    public String putFile(String objectKey, Path localPath, String contentType) {
        if (!isEnabled() || !StringUtils.hasText(objectKey) || localPath == null || !Files.isRegularFile(localPath)) {
            return null;
        }
        try {
            ensureBucket();
            client().putObject(
                    PutObjectRequest.builder()
                            .bucket(properties.getBucket())
                            .key(objectKey)
                            .contentType(StringUtils.hasText(contentType) ? contentType : "application/octet-stream")
                            .build(),
                    RequestBody.fromFile(localPath));
            return objectKey;
        } catch (Exception e) {
            log.warn("Object storage putFile failed (key={}): {}", objectKey, e.getMessage());
            return null;
        }
    }

    public String getText(String objectKey) {
        if (!isEnabled() || !StringUtils.hasText(objectKey)) {
            return null;
        }
        try (InputStream in = client().getObject(GetObjectRequest.builder()
                .bucket(properties.getBucket())
                .key(objectKey)
                .build())) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (NoSuchKeyException e) {
            return null;
        } catch (IOException e) {
            log.warn("Object storage getText failed (key={}): {}", objectKey, e.getMessage());
            return null;
        }
    }

    public InputStream getObjectStream(String objectKey) {
        if (!isEnabled() || !StringUtils.hasText(objectKey)) {
            return null;
        }
        try {
            return client().getObject(GetObjectRequest.builder()
                    .bucket(properties.getBucket())
                    .key(objectKey)
                    .build());
        } catch (Exception e) {
            log.warn("Object storage getObjectStream failed (key={}): {}", objectKey, e.getMessage());
            return null;
        }
    }

    public static String resumeObjectKey(String traceId, String extension) {
        return "resumes/" + traceId.replaceAll("[^a-zA-Z0-9\\-_]", "") + extension;
    }

    public static String llmPromptKey(String invocationId) {
        return "llm/" + invocationId + "/prompt.txt";
    }

    public static String llmResponseKey(String invocationId) {
        return "llm/" + invocationId + "/response.txt";
    }

    private S3Client client() {
        if (client == null) {
            synchronized (this) {
                if (client == null) {
                    client = S3Client.builder()
                            .endpointOverride(URI.create(properties.getEndpoint()))
                            .region(Region.of(properties.getRegion()))
                            .credentialsProvider(StaticCredentialsProvider.create(
                                    AwsBasicCredentials.create(properties.getAccessKey(), properties.getSecretKey())))
                            .serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(true).build())
                            .build();
                }
            }
        }
        return client;
    }

    private void ensureBucket() {
        try {
            client().createBucket(b -> b.bucket(properties.getBucket()));
        } catch (Exception ignored) {
            // bucket may already exist
        }
    }
}
