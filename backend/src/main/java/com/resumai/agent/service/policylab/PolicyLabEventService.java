package com.resumai.agent.service.policylab;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.policylab.PolicyExperimentEventView;
import com.resumai.agent.dao.PolicyExperimentEventMapper;
import com.resumai.agent.domain.entity.PolicyExperimentEvent;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class PolicyLabEventService {

    private final PolicyExperimentEventMapper eventMapper;
    private final ObjectMapper objectMapper;

    public PolicyLabEventService(PolicyExperimentEventMapper eventMapper,
                                 ObjectMapper objectMapper) {
        this.eventMapper = eventMapper;
        this.objectMapper = objectMapper;
    }

    @Transactional
    public PolicyExperimentEvent emit(String experimentId, String eventType, Map<String, Object> payload) {
        int nextSeq = nextSeq(experimentId);
        PolicyExperimentEvent row = new PolicyExperimentEvent();
        row.setExperimentId(experimentId);
        row.setSeq(nextSeq);
        row.setEventType(eventType);
        row.setPayload(writeJson(payload != null ? payload : Map.of()));
        row.setCreateTime(LocalDateTime.now());
        eventMapper.insert(row);
        return row;
    }

    public List<PolicyExperimentEventView> listAfter(String experimentId, int afterSeq, int limit) {
        int cap = Math.max(1, Math.min(limit, 500));
        List<PolicyExperimentEvent> rows = eventMapper.selectList(
                new QueryWrapper<PolicyExperimentEvent>()
                        .eq("experiment_id", experimentId)
                        .gt("seq", afterSeq)
                        .orderByAsc("seq")
                        .last("limit " + cap));
        return rows.stream().map(this::toView).toList();
    }

    private int nextSeq(String experimentId) {
        PolicyExperimentEvent latest = eventMapper.selectOne(
                new QueryWrapper<PolicyExperimentEvent>()
                        .eq("experiment_id", experimentId)
                        .orderByDesc("seq")
                        .last("limit 1"));
        return latest == null || latest.getSeq() == null ? 1 : latest.getSeq() + 1;
    }

    private PolicyExperimentEventView toView(PolicyExperimentEvent row) {
        return new PolicyExperimentEventView(
                row.getId(),
                row.getExperimentId(),
                row.getSeq(),
                row.getEventType(),
                readMap(row.getPayload()),
                row.getCreateTime());
    }

    private Map<String, Object> readMap(String json) {
        if (json == null || json.isBlank()) {
            return Map.of();
        }
        try {
            return objectMapper.readValue(json, new TypeReference<>() {
            });
        } catch (Exception e) {
            Map<String, Object> fallback = new LinkedHashMap<>();
            fallback.put("raw", json);
            return fallback;
        }
    }

    private String writeJson(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            return "{}";
        }
    }
}
