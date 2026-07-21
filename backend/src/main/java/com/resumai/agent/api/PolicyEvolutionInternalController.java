package com.resumai.agent.api;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.PolicyBundleMapper;
import com.resumai.agent.dao.PolicyEvolutionLogMapper;
import com.resumai.agent.domain.entity.PolicyBundleRow;
import com.resumai.agent.domain.entity.PolicyEvolutionLogRow;
import com.resumai.agent.service.InternalWorkflowService;
import com.resumai.agent.service.run.PolicyService;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/**
 * 策略自进化环路的控制面（仅 Docker 内网 + 内部令牌）：
 * evolve_policies.py 通过这些端点创建候选变体、记录 held-out 得分、
 * 晋升 champion 或淘汰候选。每一步都写 policy_evolution_log 审计。
 */
@RestController
@RequestMapping("/api/internal/policies")
public class PolicyEvolutionInternalController {

    private final InternalWorkflowService internalWorkflowService;
    private final PolicyBundleMapper bundleMapper;
    private final PolicyEvolutionLogMapper evolutionLogMapper;
    private final PolicyService policyService;
    private final ObjectMapper objectMapper;

    public PolicyEvolutionInternalController(InternalWorkflowService internalWorkflowService,
                                             PolicyBundleMapper bundleMapper,
                                             PolicyEvolutionLogMapper evolutionLogMapper,
                                             PolicyService policyService,
                                             ObjectMapper objectMapper) {
        this.internalWorkflowService = internalWorkflowService;
        this.bundleMapper = bundleMapper;
        this.evolutionLogMapper = evolutionLogMapper;
        this.policyService = policyService;
        this.objectMapper = objectMapper;
    }

    private void authorize(String token) {
        if (!internalWorkflowService.authorize(token)) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "invalid internal token");
        }
    }

    @GetMapping
    public List<Map<String, Object>> listBundles(@RequestHeader("X-Internal-Token") String token) {
        authorize(token);
        return bundleMapper.selectList(new QueryWrapper<PolicyBundleRow>()
                        .in("status", "ACTIVE", "CANDIDATE"))
                .stream().map(this::view).toList();
    }

    public record CandidateRequest(String policyId, String name, String description,
                                   Map<String, Object> config, String parentPolicyId,
                                   Integer generation, String mutationReason) {
    }

    /** LLM 反思变异产出的候选策略：status=CANDIDATE，不参与线上选择。 */
    @PostMapping("/candidates")
    public Map<String, Object> createCandidate(@RequestHeader("X-Internal-Token") String token,
                                               @RequestBody CandidateRequest request) {
        authorize(token);
        if (request.policyId() == null || request.config() == null) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "policyId and config required");
        }
        PolicyBundleRow row = new PolicyBundleRow();
        row.setPolicyId(request.policyId());
        row.setName(request.name() != null ? request.name() : request.policyId());
        row.setDescription(request.description());
        row.setConfig(writeJson(request.config()));
        row.setStatus("CANDIDATE");
        row.setIsChampion(0);
        row.setParentPolicyId(request.parentPolicyId());
        row.setGeneration(request.generation() != null ? request.generation() : 1);
        row.setVersion(1);
        row.setCreateTime(LocalDateTime.now());
        row.setUpdateTime(LocalDateTime.now());
        bundleMapper.insert(row);
        logEvolution(row.getGeneration(), row.getPolicyId(), row.getParentPolicyId(),
                "CANDIDATE_CREATED", request.mutationReason(), null, null, null);
        return Map.of("status", "OK", "policyId", row.getPolicyId());
    }

    public record VerdictRequest(Integer generation, Double benchmarkScore,
                                 Double championScore, String reason, Boolean promote) {
    }

    /**
     * held-out 验证后的裁决：promote=true 时候选转 ACTIVE 并成为 champion；
     * 否则 RETIRED。老 champion 保持 ACTIVE 可随时回滚（markChampion 幂等）。
     */
    @PostMapping("/{policyId}/verdict")
    public Map<String, Object> verdict(@RequestHeader("X-Internal-Token") String token,
                                       @PathVariable String policyId,
                                       @RequestBody VerdictRequest request) {
        authorize(token);
        PolicyBundleRow row = bundleMapper.selectById(policyId);
        if (row == null) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "policy not found: " + policyId);
        }
        boolean promote = Boolean.TRUE.equals(request.promote());
        UpdateWrapper<PolicyBundleRow> update = new UpdateWrapper<>();
        update.eq("policy_id", policyId)
                .set("status", promote ? "ACTIVE" : "RETIRED")
                .set("update_time", LocalDateTime.now());
        bundleMapper.update(null, update);
        if (promote) {
            policyService.markChampion(policyId);
        }
        logEvolution(request.generation(), policyId, row.getParentPolicyId(),
                promote ? "PROMOTED" : "REJECTED", request.reason(),
                request.benchmarkScore(), request.championScore(), null);
        return Map.of("status", "OK", "policyId", policyId,
                "action", promote ? "PROMOTED" : "REJECTED");
    }

    @GetMapping("/evolution-log")
    public List<PolicyEvolutionLogRow> evolutionLog(@RequestHeader("X-Internal-Token") String token) {
        authorize(token);
        return evolutionLogMapper.selectList(new QueryWrapper<PolicyEvolutionLogRow>()
                .orderByDesc("id").last("limit 100"));
    }

    private void logEvolution(Integer generation, String policyId, String parentPolicyId,
                              String action, String reason, Double benchmarkScore,
                              Double championScore, String detail) {
        PolicyEvolutionLogRow log = new PolicyEvolutionLogRow();
        log.setGeneration(generation != null ? generation : 0);
        log.setPolicyId(policyId);
        log.setParentPolicyId(parentPolicyId);
        log.setAction(action);
        log.setMutationReason(reason);
        log.setBenchmarkScore(benchmarkScore != null ? BigDecimal.valueOf(benchmarkScore) : null);
        log.setChampionScore(championScore != null ? BigDecimal.valueOf(championScore) : null);
        log.setDetail(detail);
        log.setCreateTime(LocalDateTime.now());
        evolutionLogMapper.insert(log);
    }

    private Map<String, Object> view(PolicyBundleRow row) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("policyId", row.getPolicyId());
        view.put("name", row.getName());
        view.put("status", row.getStatus());
        view.put("isChampion", row.getIsChampion());
        view.put("parentPolicyId", row.getParentPolicyId());
        view.put("generation", row.getGeneration());
        view.put("config", readJson(row.getConfig()));
        return view;
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "{}";
        }
    }

    private Object readJson(String json) {
        try {
            return json != null ? objectMapper.readValue(json, Map.class) : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }
}
