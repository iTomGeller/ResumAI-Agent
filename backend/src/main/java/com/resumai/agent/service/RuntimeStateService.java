package com.resumai.agent.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.TaskResponse;
import java.time.Duration;
import java.util.Optional;
import org.redisson.api.RBucket;
import org.redisson.api.RedissonClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Redis 运行态缓存 — 承接 RUNNING 任务快照与 SSE 游标，非长期事实库。
 */
@Service
public class RuntimeStateService {

    private static final Logger log = LoggerFactory.getLogger(RuntimeStateService.class);
    private static final Duration RUNNING_TTL = Duration.ofHours(24);
    private static final Duration SSE_CURSOR_TTL = Duration.ofHours(6);

    private final RedissonClient redissonClient;
    private final ObjectMapper objectMapper;

    public RuntimeStateService(RedissonClient redissonClient, ObjectMapper objectMapper) {
        this.redissonClient = redissonClient;
        this.objectMapper = objectMapper;
    }

    public void cacheRunningTask(TaskResponse task) {
        if (task == null || !StringUtils.hasText(task.traceId()) || !"RUNNING".equals(task.status())) {
            return;
        }
        try {
            RBucket<String> bucket = redissonClient.getBucket(key("task", task.traceId()));
            bucket.set(objectMapper.writeValueAsString(task), RUNNING_TTL);
        } catch (Exception e) {
            log.debug("Redis cache running task failed (trace={}): {}", task.traceId(), e.getMessage());
        }
    }

    public Optional<TaskResponse> getRunningTask(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return Optional.empty();
        }
        try {
            RBucket<String> bucket = redissonClient.getBucket(key("task", traceId));
            String json = bucket.get();
            if (!StringUtils.hasText(json)) {
                return Optional.empty();
            }
            return Optional.of(objectMapper.readValue(json, TaskResponse.class));
        } catch (Exception e) {
            log.debug("Redis get running task failed (trace={}): {}", traceId, e.getMessage());
            return Optional.empty();
        }
    }

    public void evictRunningTask(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return;
        }
        try {
            redissonClient.getBucket(key("task", traceId)).delete();
        } catch (Exception e) {
            log.debug("Redis evict running task failed (trace={}): {}", traceId, e.getMessage());
        }
    }

    public void saveSseCursor(String traceId, String spanId) {
        if (!StringUtils.hasText(traceId) || !StringUtils.hasText(spanId)) {
            return;
        }
        try {
            redissonClient.getBucket(key("sse", traceId)).set(spanId, SSE_CURSOR_TTL);
        } catch (Exception e) {
            log.debug("Redis save SSE cursor failed (trace={}): {}", traceId, e.getMessage());
        }
    }

    public Optional<String> getSseCursor(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return Optional.empty();
        }
        try {
            String cursor = redissonClient.<String>getBucket(key("sse", traceId)).get();
            return Optional.ofNullable(cursor).filter(StringUtils::hasText);
        } catch (Exception e) {
            return Optional.empty();
        }
    }

    private static String key(String type, String traceId) {
        return "resumai:runtime:" + type + ":" + traceId;
    }
}
