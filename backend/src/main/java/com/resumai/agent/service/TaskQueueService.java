package com.resumai.agent.service;

import com.resumai.agent.config.TaskQueueProperties;
import jakarta.annotation.PostConstruct;
import java.time.Duration;
import java.util.Map;
import java.util.Optional;
import org.redisson.api.RStream;
import org.redisson.api.RedissonClient;
import org.redisson.api.StreamMessageId;
import org.redisson.api.stream.StreamAddArgs;
import org.redisson.api.stream.StreamCreateGroupArgs;
import org.redisson.api.stream.StreamReadGroupArgs;
import org.redisson.client.codec.StringCodec;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class TaskQueueService {

    private static final Logger log = LoggerFactory.getLogger(TaskQueueService.class);

    private final RedissonClient redissonClient;
    private final TaskQueueProperties properties;

    public TaskQueueService(RedissonClient redissonClient, TaskQueueProperties properties) {
        this.redissonClient = redissonClient;
        this.properties = properties;
    }

    @PostConstruct
    public void ensureConsumerGroup() {
        if (!properties.isEnabled()) {
            return;
        }
        try {
            stream().createGroup(StreamCreateGroupArgs.name(properties.getConsumerGroup()).makeStream());
            log.info("Created Redis Stream consumer group {}", properties.getConsumerGroup());
        } catch (Exception e) {
            log.debug("Consumer group may already exist: {}", e.getMessage());
        }
    }

    public void enqueue(String traceId, Long taskId, String tenantId, String uploadedBy, int priority) {
        if (!StringUtils.hasText(traceId)) {
            return;
        }
        if (!properties.isEnabled()) {
            log.warn("Task queue disabled, skip enqueue trace={}", traceId);
            return;
        }
        Map<String, String> body = Map.of(
                "traceId", traceId,
                "taskId", taskId != null ? String.valueOf(taskId) : "0",
                "tenantId", StringUtils.hasText(tenantId) ? tenantId : "default",
                "uploadedBy", StringUtils.hasText(uploadedBy) ? uploadedBy : "demo-hr",
                "priority", String.valueOf(priority)
        );
        stream().add(StreamAddArgs.entries(body));
    }

    public Optional<QueuedTaskMessage> pollMessage() {
        if (!properties.isEnabled()) {
            return Optional.empty();
        }
        Map<StreamMessageId, Map<String, String>> messages = stream().readGroup(
                properties.getConsumerGroup(),
                properties.getWorkerId(),
                StreamReadGroupArgs.neverDelivered().count(1).timeout(Duration.ofMillis(200))
        );
        if (messages == null || messages.isEmpty()) {
            return Optional.empty();
        }
        Map.Entry<StreamMessageId, Map<String, String>> entry = messages.entrySet().iterator().next();
        Map<String, String> body = entry.getValue();
        String traceId = body.get("traceId");
        if (!StringUtils.hasText(traceId)) {
            ack(entry.getKey());
            return Optional.empty();
        }
        return Optional.of(new QueuedTaskMessage(entry.getKey(), traceId, body.get("uploadedBy")));
    }

    public void ack(StreamMessageId messageId) {
        if (messageId == null || !properties.isEnabled()) {
            return;
        }
        stream().ack(properties.getConsumerGroup(), messageId);
    }

    public long pendingCount() {
        if (!properties.isEnabled()) {
            return 0L;
        }
        try {
            var pending = stream().getPendingInfo(properties.getConsumerGroup());
            return pending != null ? pending.getTotal() : 0L;
        } catch (Exception e) {
            return 0L;
        }
    }

    public long streamSize() {
        if (!properties.isEnabled()) {
            return 0L;
        }
        try {
            return stream().size();
        } catch (Exception e) {
            return 0L;
        }
    }

    public TaskQueueProperties properties() {
        return properties;
    }

    private RStream<String, String> stream() {
        return redissonClient.getStream(properties.getStreamKey(), StringCodec.INSTANCE);
    }

    public record QueuedTaskMessage(StreamMessageId messageId, String traceId, String uploadedBy) {
    }
}
