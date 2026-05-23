package com.resumai.agent.api;

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
                "time", LocalDateTime.now()
        );
    }
}
