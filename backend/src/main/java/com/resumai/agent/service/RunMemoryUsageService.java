package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.resumai.agent.dao.RunMemoryUsageMapper;
import com.resumai.agent.domain.entity.RunMemoryUsageRow;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Persists per-run memory USED/IGNORED decisions for Ops drilldown.
 */
@Service
public class RunMemoryUsageService {

    private static final Logger log = LoggerFactory.getLogger(RunMemoryUsageService.class);

    private final RunMemoryUsageMapper mapper;

    public RunMemoryUsageService(RunMemoryUsageMapper mapper) {
        this.mapper = mapper;
    }

    public record UsageDecision(String memoryId, String consumerAgent, Integer rankNo,
                                Double vectorScore, Double lexicalScore, Double recencyScore,
                                Double finalScore, String decision, String ignoredReason) {
    }

    public int recordUsage(String runId, String consumerAgent, List<UsageDecision> decisions) {
        if (!StringUtils.hasText(runId) || decisions == null || decisions.isEmpty()) {
            return 0;
        }
        int written = 0;
        LocalDateTime now = LocalDateTime.now();
        for (UsageDecision d : decisions) {
            if (d == null || !StringUtils.hasText(d.memoryId())) {
                continue;
            }
            RunMemoryUsageRow row = new RunMemoryUsageRow();
            row.setRunId(runId.trim());
            row.setMemoryId(d.memoryId().trim());
            row.setConsumerAgent(StringUtils.hasText(d.consumerAgent())
                    ? d.consumerAgent().trim()
                    : (StringUtils.hasText(consumerAgent) ? consumerAgent.trim() : "UNKNOWN"));
            row.setRankNo(d.rankNo());
            row.setVectorScore(decimal(d.vectorScore()));
            row.setLexicalScore(decimal(d.lexicalScore()));
            row.setRecencyScore(decimal(d.recencyScore()));
            row.setFinalScore(decimal(d.finalScore()));
            String decision = normalizeDecision(d.decision());
            row.setDecision(decision);
            row.setIgnoredReason("IGNORED".equals(decision) ? truncate(d.ignoredReason(), 250) : null);
            row.setCreateTime(now);
            try {
                mapper.insert(row);
                written++;
            } catch (Exception e) {
                log.debug("run_memory_usage insert skipped run={} mem={}: {}",
                        runId, d.memoryId(), e.getMessage());
            }
        }
        return written;
    }

    @SuppressWarnings("unchecked")
    public int recordUsageFromPayload(String runId, Map<String, Object> body) {
        if (body == null) {
            return 0;
        }
        String consumer = body.get("consumerAgent") != null
                ? String.valueOf(body.get("consumerAgent")) : null;
        Object raw = body.get("decisions");
        if (!(raw instanceof List<?> list)) {
            return 0;
        }
        List<UsageDecision> decisions = new ArrayList<>();
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> map)) {
                continue;
            }
            decisions.add(new UsageDecision(
                    str(map.get("memoryId")),
                    str(map.get("consumerAgent")),
                    intOrNull(map.get("rankNo") != null ? map.get("rankNo") : map.get("rank")),
                    doubleOrNull(map.get("vectorScore")),
                    doubleOrNull(map.get("lexicalScore")),
                    doubleOrNull(map.get("recencyScore")),
                    doubleOrNull(map.get("finalScore") != null ? map.get("finalScore") : map.get("score")),
                    str(map.get("decision")),
                    str(map.get("ignoredReason") != null ? map.get("ignoredReason") : map.get("ignored_reason"))
            ));
        }
        return recordUsage(runId, consumer, decisions);
    }

    public List<RunMemoryUsageRow> listForRun(String runId, String decision, int limit) {
        int cap = Math.max(1, Math.min(limit, 500));
        QueryWrapper<RunMemoryUsageRow> q = new QueryWrapper<RunMemoryUsageRow>()
                .eq("run_id", runId)
                .orderByAsc("rank_no")
                .orderByDesc("create_time")
                .last("limit " + cap);
        if (StringUtils.hasText(decision)) {
            q.eq("decision", decision.trim().toUpperCase(Locale.ROOT));
        }
        return mapper.selectList(q);
    }

    public List<RunMemoryUsageRow> listRecent(String runId, String decision, int limit) {
        int cap = Math.max(1, Math.min(limit, 500));
        QueryWrapper<RunMemoryUsageRow> q = new QueryWrapper<RunMemoryUsageRow>()
                .orderByDesc("create_time")
                .last("limit " + cap);
        if (StringUtils.hasText(runId)) {
            q.eq("run_id", runId.trim());
        }
        if (StringUtils.hasText(decision)) {
            q.eq("decision", decision.trim().toUpperCase(Locale.ROOT));
        }
        return mapper.selectList(q);
    }

    private static String normalizeDecision(String raw) {
        if (!StringUtils.hasText(raw)) {
            return "USED";
        }
        String key = raw.trim().toUpperCase(Locale.ROOT);
        if ("IGNORED".equals(key) || "FALSE".equals(key) || "0".equals(key)) {
            return "IGNORED";
        }
        return "USED";
    }

    private static BigDecimal decimal(Double value) {
        if (value == null || value.isNaN() || value.isInfinite()) {
            return null;
        }
        return BigDecimal.valueOf(value).setScale(5, RoundingMode.HALF_UP);
    }

    private static String str(Object value) {
        return value == null ? null : String.valueOf(value);
    }

    private static Integer intOrNull(Object value) {
        if (value instanceof Number n) {
            return n.intValue();
        }
        if (value == null) {
            return null;
        }
        try {
            return Integer.parseInt(String.valueOf(value));
        } catch (Exception e) {
            return null;
        }
    }

    private static Double doubleOrNull(Object value) {
        if (value instanceof Number n) {
            return n.doubleValue();
        }
        if (value == null) {
            return null;
        }
        try {
            return Double.parseDouble(String.valueOf(value));
        } catch (Exception e) {
            return null;
        }
    }

    private static String truncate(String value, int max) {
        if (value == null) {
            return null;
        }
        return value.length() <= max ? value : value.substring(0, max);
    }
}
