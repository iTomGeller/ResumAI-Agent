package com.resumai.agent.api;

import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.config.EmbeddingProperties;
import com.resumai.agent.config.LangfuseHealthService;
import com.resumai.agent.config.WorkflowProperties;
import com.resumai.agent.service.ResumeRagService;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
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
    private final ResumeRagService resumeRagService;
    private final WorkflowProperties workflowProperties;
    private final LangfuseHealthService langfuseHealth;

    public HealthController(EmbeddingAvailability embeddingAvailability,
                            EmbeddingProperties embeddingProperties,
                            ResumeRagService resumeRagService,
                            WorkflowProperties workflowProperties,
                            LangfuseHealthService langfuseHealth) {
        this.embeddingAvailability = embeddingAvailability;
        this.embeddingProperties = embeddingProperties;
        this.resumeRagService = resumeRagService;
        this.workflowProperties = workflowProperties;
        this.langfuseHealth = langfuseHealth;
    }

    /**
     * 返回后端健康状态。
     *
     * @return 健康状态
     */
    @GetMapping("/health")
    public Map<String, Object> health() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "UP");
        body.put("service", "resumai-agent-backend");
        body.put("time", LocalDateTime.now());
        body.put("embedding", Map.of(
                "operational", embeddingAvailability.isOperational(),
                "provider", embeddingProperties.getProvider() == null ? "local" : embeddingProperties.getProvider(),
                "message", embeddingAvailability.statusMessage()
        ));
        body.put("langfuse", langfuseHealth.snapshot());
        return body;
    }

    @GetMapping("/health/dependencies")
    public Map<String, Object> dependencies() {
        Map<String, Object> deps = new LinkedHashMap<>();
        deps.put("embedding", Map.of(
                "operational", embeddingAvailability.isOperational(),
                "message", embeddingAvailability.statusMessage()
        ));
        boolean milvusAvailable = resumeRagService.isMilvusAvailable();
        Map<String, Object> milvus = new LinkedHashMap<>();
        milvus.put("available", milvusAvailable);
        milvus.put("lastError", milvusAvailable ? "" : "milvus_unavailable");
        deps.put("milvus", milvus);
        boolean mcpAuthorized = workflowProperties.getInternalToken() != null
                && !workflowProperties.getInternalToken().isBlank();
        deps.put("workflow", Map.of(
                "mcpResumeToolsAuthorized", mcpAuthorized
        ));
        deps.put("langfuse", langfuseHealth.snapshot());
        return Map.of("status", "UP", "dependencies", deps);
    }
}
