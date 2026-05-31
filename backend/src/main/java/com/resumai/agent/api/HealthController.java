package com.resumai.agent.api;

import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.config.EmbeddingProperties;
import java.time.LocalDateTime;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * API 健康检查控制器。
 */
@RestController
@RequestMapping("/api")
public class HealthController {

    private final EmbeddingAvailability embeddingAvailability;
    private final EmbeddingProperties embeddingProperties;

    public HealthController(EmbeddingAvailability embeddingAvailability,
                            EmbeddingProperties embeddingProperties) {
        this.embeddingAvailability = embeddingAvailability;
        this.embeddingProperties = embeddingProperties;
    }

    /**
     * 返回后端健康状态。
     *
     * @return 健康状态
     */
    @GetMapping("/health")
    public Map<String, Object> health() {
        return Map.of(
                "status", "UP",
                "service", "resumai-agent-backend",
                "time", LocalDateTime.now(),
                "embedding", Map.of(
                        "operational", embeddingAvailability.isOperational(),
                        "provider", embeddingProperties.getProvider() == null ? "local" : embeddingProperties.getProvider(),
                        "message", embeddingAvailability.statusMessage()
                )
        );
    }
}
