package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.SystemOrchestrationRuleMapper;
import com.resumai.agent.domain.entity.SystemOrchestrationRule;
import com.resumai.agent.rag.RagOptions;
import java.time.LocalDateTime;
import java.util.concurrent.atomic.AtomicReference;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

@Service
public class RagConfigService {

    private static final Logger log = LoggerFactory.getLogger(RagConfigService.class);
    private static final String GLOBAL_CATEGORY = "__RAG_DEFAULT__";

    private final SystemOrchestrationRuleMapper ruleMapper;
    private final ObjectMapper objectMapper;
    private final AtomicReference<RagOptions> cached = new AtomicReference<>();

    public RagConfigService(SystemOrchestrationRuleMapper ruleMapper, ObjectMapper objectMapper) {
        this.ruleMapper = ruleMapper;
        this.objectMapper = objectMapper;
    }

    public RagOptions getDefaultOptions() {
        RagOptions current = cached.get();
        if (current != null) {
            return current;
        }
        RagOptions loaded = loadFromDb().orElse(loadBalancedPreset());
        cached.set(loaded);
        return loaded;
    }

    public RagOptions saveDefaultOptions(RagOptions options) {
        RagOptions normalized = options != null ? options : RagOptions.defaults();
        cached.set(normalized);
        persistToDb(normalized);
        return normalized;
    }

    public void invalidateCache() {
        cached.set(null);
    }

    private java.util.Optional<RagOptions> loadFromDb() {
        try {
            SystemOrchestrationRule rule = ruleMapper.selectOne(
                    new QueryWrapper<SystemOrchestrationRule>()
                            .eq("job_category", GLOBAL_CATEGORY)
                            .eq("enabled", 1)
                            .orderByDesc("version")
                            .last("LIMIT 1"));
            if (rule == null || !StringUtils.hasText(rule.getExecutionPolicy())) {
                return java.util.Optional.empty();
            }
            return java.util.Optional.of(objectMapper.readValue(rule.getExecutionPolicy(), RagOptions.class));
        } catch (Exception e) {
            log.warn("Failed to load rag.default.options: {}", e.getMessage());
            return java.util.Optional.empty();
        }
    }

    private void persistToDb(RagOptions options) {
        try {
            String json = objectMapper.writeValueAsString(options);
            SystemOrchestrationRule existing = ruleMapper.selectOne(
                    new QueryWrapper<SystemOrchestrationRule>()
                            .eq("job_category", GLOBAL_CATEGORY)
                            .orderByDesc("version")
                            .last("LIMIT 1"));
            LocalDateTime now = LocalDateTime.now();
            if (existing == null) {
                SystemOrchestrationRule rule = new SystemOrchestrationRule();
                rule.setId(System.currentTimeMillis());
                rule.setJobCategory(GLOBAL_CATEGORY);
                rule.setPreferredRagStrategy(options.strategy());
                rule.setTopK(options.topK());
                rule.setExecutionPolicy(json);
                rule.setEnabled(1);
                rule.setVersion(1);
                rule.setCreateTime(now);
                rule.setUpdateTime(now);
                rule.setDeleted(0);
                ruleMapper.insert(rule);
            } else {
                existing.setPreferredRagStrategy(options.strategy());
                existing.setTopK(options.topK());
                existing.setExecutionPolicy(json);
                existing.setUpdateTime(now);
                ruleMapper.updateById(existing);
            }
        } catch (Exception e) {
            log.warn("Failed to persist rag.default.options: {}", e.getMessage());
        }
    }

    private RagOptions loadBalancedPreset() {
        try {
            var resource = new ClassPathResource("rag-presets.json");
            var root = objectMapper.readTree(resource.getInputStream());
            for (var preset : root.get("presets")) {
                if (preset.path("default").asBoolean(false)) {
                    return objectMapper.treeToValue(preset.get("options"), RagOptions.class);
                }
            }
        } catch (Exception e) {
            log.warn("Failed to load rag-presets.json: {}", e.getMessage());
        }
        return RagOptions.defaults();
    }
}
