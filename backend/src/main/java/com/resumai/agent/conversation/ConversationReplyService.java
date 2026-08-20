package com.resumai.agent.conversation;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ContextRefRequest;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.dao.ContextSnapshotMapper;
import com.resumai.agent.dao.ConversationMessageMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ContextSnapshotRow;
import com.resumai.agent.domain.entity.ConversationMessage;
import com.resumai.agent.domain.entity.ConversationSession;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.redisson.api.RMapCache;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Standalone Java Copilot. It never enters the Python workflow, creates an
 * AgentRun, or produces ReportAgent output.
 */
@Service
public class ConversationReplyService {

    private static final Logger log = LoggerFactory.getLogger(ConversationReplyService.class);
    private static final Pattern SIMPLE_ARITH =
            Pattern.compile("^\\s*(\\d+)\\s*([+\\-*/x×])\\s*(\\d+)\\s*$");

    private static final Duration HISTORY_CACHE_TTL = Duration.ofHours(2);
    private static final int HISTORY_CONTEXT_HIGH_WATERMARK = 2400;
    private static final int HISTORY_CONTEXT_TARGET_TOKEN_LIMIT = 1600;
    private static final int MAX_RECENT_MESSAGE_COUNT = 64;

    private final CopilotLlmClient llmClient;
    private final AgentRunMapper agentRunMapper;
    private final ConversationMessageMapper conversationMessageMapper;
    private final ContextSnapshotMapper contextSnapshotMapper;
    private final ObjectMapper objectMapper;
    private final RedissonClient redisson;
    private final CopilotMetrics metrics;

    @Autowired
    public ConversationReplyService(CopilotLlmClient llmClient,
                                    AgentRunMapper agentRunMapper,
                                    ConversationMessageMapper conversationMessageMapper,
                                    ContextSnapshotMapper contextSnapshotMapper,
                                    ObjectMapper objectMapper,
                                    RedissonClient redisson,
                                    CopilotMetrics metrics) {
        this.llmClient = llmClient;
        this.agentRunMapper = agentRunMapper;
        this.conversationMessageMapper = conversationMessageMapper;
        this.contextSnapshotMapper = contextSnapshotMapper;
        this.objectMapper = objectMapper;
        this.redisson = redisson;
        this.metrics = metrics;
    }

    /** Compatibility constructor for lightweight unit tests. */
    public ConversationReplyService(CopilotLlmClient llmClient,
                                    AgentRunMapper agentRunMapper,
                                    ConversationMessageMapper conversationMessageMapper,
                                    ContextSnapshotMapper contextSnapshotMapper,
                                    ObjectMapper objectMapper,
                                    RedissonClient redisson) {
        this(llmClient, agentRunMapper, conversationMessageMapper,
                contextSnapshotMapper, objectMapper, redisson, new CopilotMetrics());
    }

    public record CopilotReply(
            String turnId,
            String answer,
            List<Map<String, Object>> citations,
            List<Map<String, Object>> actions,
            List<String> suggestions,
            String conversationSummary
    ) {
    }

    private record HistoryContext(
            String summary,
            int summaryVersion,
            List<Map<String, Object>> messagesToCompact,
            List<Map<String, Object>> recentMessages,
            Long sourceMessageStartId,
            Long sourceMessageEndId,
            Long firstKeptMessageId,
            int beforeTokenEstimate
    ) {
    }

    private record HistoryWindow(
            List<ConversationMessage> compactRows,
            List<ConversationMessage> recentRows
    ) {
    }

    public CopilotReply reply(ConversationSession session,
                              ConversationTurnRequest request,
                              TurnDecision decision,
                              boolean allowTools) {
        return reply(session, request, decision, allowTools, null);
    }

    public CopilotReply reply(ConversationSession session,
                              ConversationTurnRequest request,
                              TurnDecision decision,
                              boolean allowTools,
                              String preferredTurnId) {
        return reply(session, request, decision, allowTools, preferredTurnId, null);
    }

    public CopilotReply reply(ConversationSession session,
                              ConversationTurnRequest request,
                              TurnDecision decision,
                              boolean allowTools,
                              String preferredTurnId,
                              Consumer<String> onDelta) {
        long startedNanos = System.nanoTime();
        String turnId = StringUtils.hasText(preferredTurnId)
                ? preferredTurnId
                : "turn-" + UUID.randomUUID();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("turnId", turnId);
        body.put("conversationId", session.getId());
        body.put("content", request.content());
        body.put("disposition", decision.disposition().name());
        body.put("intent", decision.intent());
        body.put("allowTools", allowTools);
        body.put("contextRefs", toRefMaps(request.contextRefs()));
        Map<String, Object> snapshot = new LinkedHashMap<>();
        snapshot.put("activeGoal", session.getCurrentGoal());
        snapshot.put("summary", session.getSummary());
        snapshot.put("jobCategory", session.getJobCategory());
        snapshot.put("revision", session.getActiveRevision());
        snapshot.put("hasResume", StringUtils.hasText(session.getResumeText()));
        snapshot.put("hasJobDescription", StringUtils.hasText(session.getJobDescription()));
        HistoryContext history = loadHistoryContext(
                session.getId(), request.clientMessageId());
        if (StringUtils.hasText(history.summary())) {
            snapshot.put("conversationSummary", history.summary());
        }
        if (!history.messagesToCompact().isEmpty()) {
            snapshot.put("messagesToCompact", history.messagesToCompact());
        }
        if (!history.recentMessages().isEmpty()) {
            snapshot.put("recentMessages", history.recentMessages());
        }
        if (StringUtils.hasText(session.getResumeText())) {
            snapshot.put("resumeText", clipPreservingHeadTail(session.getResumeText(), 1800));
        }
        if (StringUtils.hasText(session.getJobDescription())) {
            snapshot.put("jobDescription", clipPreservingHeadTail(session.getJobDescription(), 1600));
        }
        // Include structuredReport from the latest completed run for rich Copilot answers
        Map<String, Object> report = getLatestStructuredReport(session.getId());
        if (report != null && !report.isEmpty()) {
            snapshot.put("structuredReport", report);
        }
        body.put("contextSnapshot", snapshot);

        Optional<Map<String, Object>> remote = llmClient.reply(body, onDelta);
        if (remote.isPresent()) {
            CopilotReply reply = fromPayload(turnId, remote.get());
            persistHistorySnapshot(session.getId(), history, reply.conversationSummary());
            metrics.recordCopilotReply(true, elapsedMs(startedNanos));
            return reply;
        }
        log.debug("Java Copilot model unavailable; using local fallback");
        metrics.recordCopilotReply(false, elapsedMs(startedNanos));
        return localFallback(turnId, request.content(), decision, session);
    }

    private RMapCache<String, String> historyCache() {
        return redisson.getMapCache("resumai:copilot:context");
    }

    @SuppressWarnings("unchecked")
    private CopilotReply fromPayload(String turnId, Map<String, Object> payload) {
        String answer = String.valueOf(payload.getOrDefault("answer",
                payload.getOrDefault("assistantMessage", "")));
        if (!StringUtils.hasText(answer) || "null".equals(answer)) {
            answer = "暂时无法生成回答，请稍后再试。";
        }
        List<Map<String, Object>> citations = mapList(payload.get("citations"));
        List<Map<String, Object>> actions = mapList(payload.get("actions"));
        List<String> suggestions = stringList(payload.get("suggestions"));
        String remoteTurn = payload.get("turnId") != null
                ? String.valueOf(payload.get("turnId")) : turnId;
        String conversationSummary = payload.get("conversationSummary") != null
                ? clip(String.valueOf(payload.get("conversationSummary")), 1600) : null;
        return new CopilotReply(remoteTurn, answer, citations, actions, suggestions,
                conversationSummary);
    }

    private CopilotReply localFallback(String turnId, String content, TurnDecision decision,
                                       ConversationSession session) {
        String answer;
        String text = content == null ? "" : content.trim();
        String lower = text.toLowerCase(java.util.Locale.ROOT);
        List<String> suggestions;
        Matcher arith = SIMPLE_ARITH.matcher(text);
        if (arith.matches()) {
            answer = evaluateArithmetic(arith);
            suggestions = defaultSuggestions();
        } else if (decision.needsConfirmation()) {
            answer = "我不完全确定你的意图：是想补充当前评估的信息，还是提出一个新的目标？"
                    + "如果要改评估方向，请明确说“改为……重新评估”。";
            suggestions = List.of("继续当前评估", "改为新目标并重新评估");
        } else if (isMcpQuestion(lower)) {
            answer = "MCP 的实际链路是：运行时先完成 initialize 和 tools/list，"
                    + "把实时工具描述与 input schema 提供给模型；模型选择工具并生成参数后，"
                    + "运行时再原样发起 tools/call。只记录真实返回，工具不可用会明确报错，"
                    + "不会用伪造结果兜底。本次短答服务不可用，所以这条回复没有实际调用 MCP。";
            suggestions = List.of("查看 MCP 工具清单", "查看最近一次 MCP 调用", "重试原问题");
        } else if (isCheckpointQuestion(lower)) {
            answer = "暂停会先让当前节点到达安全边界并写入 checkpoint；恢复时沿用同一个 Run、"
                    + "同一个 revision 和该 checkpoint 继续，已完成节点不会整条重跑。"
                    + "取消与暂停不同：取消会终止该 Run，不能再按暂停流程恢复。"
                    + "这条说明本身不会改变运行状态。";
            suggestions = List.of("查看当前运行状态", "暂停当前任务", "恢复暂停任务");
        } else if (isRevisionQuestion(lower)) {
            answer = "修改 JD 或评估重点会创建新 revision；进行中的旧 Run 会被标记为已取代，"
                    + "旧结果不再作为当前结论。系统只失效并重跑受影响节点，未受影响的产物可复用。"
                    + "这条说明本身不会创建 revision 或 Run，只有明确提交变更或要求重新评估才会执行。";
            suggestions = List.of("查看当前 revision", "说明新的 JD/评估重点", "查看受影响节点");
        } else if (isEvidenceGapQuestion(lower)) {
            answer = "证据不足时不会补猜候选人的能力、经历或分数：相关维度应标记为 UNASSESSED，"
                    + "保留证据缺口并给出待核验项或面试追问；影响录用判断时转人工复核。"
                    + "没有可核验证据，就不能给确定性候选人结论。";
            suggestions = List.of("查看证据缺口", "生成核验问题", "补充候选人材料");
        } else if (isRetrievalQuestion(lower, decision)) {
            answer = "本次短答服务不可用，因此没有实际执行 RAG 或公网检索，我不会伪造命中。"
                    + "正常链路依次是 Query Rewrite、候选召回、融合去重和重排；"
                    + "详情会记录各阶段耗时、候选数、返回数、Top Score 与降级原因。"
                    + "要获取具体证据，请稍后重试原问题。";
            suggestions = List.of("重试这次证据检索", "查看 RAG 各阶段指标", "查看证据缺口");
        } else if (isCandidateResultQuestion(lower)) {
            Map<String, Object> report = getLatestStructuredReport(session.getId());
            if (report != null && report.containsKey("recommendation")) {
                String rec = String.valueOf(report.getOrDefault("recommendation", ""));
                String recLabel = switch (rec) {
                    case "HIRE" -> "推荐录用";
                    case "INTERVIEW_RECOMMEND" -> "建议面试";
                    case "NEED_MANUAL_REVIEW" -> "需人工复审";
                    case "NOT_RECOMMEND" -> "不推荐";
                    default -> rec;
                };
                answer = buildRichReportAnswer(report, recLabel);
            } else {
                answer = "当前没有可用的证据化评估报告，因此不能可靠地给候选人打分或给出录用建议，"
                        + "我也不会根据零散上下文猜测结论。完成评估后，可在决策报告页查看证据链与结论。";
            }
            suggestions = defaultSuggestions();
        } else {
            answer = "已收到。这是对话回复，不会启动完整评估流水线。"
                    + "若要重新评估，请明确说明岗位/事实变更；若要停止任务，请说“停止”。";
            suggestions = defaultSuggestions();
        }
        return new CopilotReply(turnId, answer, List.of(), List.of(), suggestions, null);
    }

    private HistoryContext loadHistoryContext(String conversationId,
                                              String currentClientMessageId) {
        return loadHistoryContext(conversationId, currentClientMessageId, true);
    }

    private HistoryContext loadHistoryContext(String conversationId,
                                              String currentClientMessageId,
                                              boolean observeRequestLookup) {
        try {
            String cached = historyCache().get(conversationId);
            if (StringUtils.hasText(cached)) {
                HistoryContext context = objectMapper.readValue(cached, HistoryContext.class);
                if (observeRequestLookup) {
                    metrics.recordContextCacheHit();
                }
                return context;
            }
            if (observeRequestLookup) {
                metrics.recordContextCacheMiss();
            }
        } catch (Exception e) {
            if (observeRequestLookup) {
                metrics.recordContextCacheMiss();
            }
            log.debug("Copilot history cache miss {}: {}",
                    conversationId, e.getMessage());
        }
        try {
            ContextSnapshotRow latest = contextSnapshotMapper.selectOne(
                    new LambdaQueryWrapper<ContextSnapshotRow>()
                    .eq(ContextSnapshotRow::getConversationId, conversationId)
                            .in(ContextSnapshotRow::getReason, List.of(
                                    "copilot_incremental_window_8",
                                    "copilot_incremental_window_token_cap",
                                    "copilot_token_budget_2400",
                                    "copilot_token_budget_2400_target_1600"))
                            .orderByDesc(ContextSnapshotRow::getId)
                            .last("limit 1"));
            Long compactedThrough = latest != null
                    ? latest.getSourceMessageEndId() : null;
            LambdaQueryWrapper<ConversationMessage> query =
                    new LambdaQueryWrapper<ConversationMessage>()
                            .eq(ConversationMessage::getConversationId, conversationId)
                            .orderByAsc(ConversationMessage::getId);
            if (compactedThrough != null) {
                query.gt(ConversationMessage::getId, compactedThrough);
            }
            if (StringUtils.hasText(currentClientMessageId)) {
                query.ne(ConversationMessage::getClientMessageId,
                        currentClientMessageId);
            }
            List<ConversationMessage> rows = conversationMessageMapper.selectList(query)
                    .stream()
                    .filter(row -> "USER".equalsIgnoreCase(row.getRole())
                            || "ASSISTANT".equalsIgnoreCase(row.getRole()))
                    .toList();
            HistoryWindow selected = selectHistoryWindow(rows,
                    latest != null ? latest.getSummary() : null);
            List<ConversationMessage> compactRows = selected.compactRows();
            List<ConversationMessage> recentRows = selected.recentRows();
            List<Map<String, Object>> toCompact = compactRows.stream()
                    .map(row -> historyMessage(row, 400)).toList();
            List<Map<String, Object>> recent = recentRows.stream()
                    .map(row -> historyMessage(row, 600)).toList();
            String previousSummary = latest != null ? latest.getSummary() : null;
            int beforeTokens = estimateTokens(previousSummary, toCompact, recent);
            HistoryContext context = new HistoryContext(
                    previousSummary,
                    latest != null && latest.getSummaryVersion() != null
                            ? latest.getSummaryVersion() : 0,
                    toCompact,
                    recent,
                    compactRows.isEmpty() ? null : compactRows.get(0).getId(),
                    compactRows.isEmpty() ? null
                            : compactRows.get(compactRows.size() - 1).getId(),
                    recentRows.isEmpty() ? null : recentRows.get(0).getId(),
                    beforeTokens);
            try {
                historyCache().fastPut(conversationId,
                        objectMapper.writeValueAsString(context),
                        HISTORY_CACHE_TTL.toMillis(), TimeUnit.MILLISECONDS);
            } catch (Exception e) {
                log.debug("Copilot history cache write skipped {}: {}",
                        conversationId, e.getMessage());
            }
            metrics.recordContextCacheRebuild();
            return context;
        } catch (Exception e) {
            log.info("Copilot history context degraded to current turn: {}", e.getMessage());
            return new HistoryContext(null, 0, List.of(), List.of(),
                    null, null, null, 0);
        }
    }

    private static long elapsedMs(long startedNanos) {
        return Math.max(0, TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedNanos));
    }

    /**
     * Keep all complete turns until the high watermark is crossed. Once crossed,
     * compact enough old turns to return near the lower target. The gap prevents
     * paying for another summary after only one or two new turns.
     */
    private HistoryWindow selectHistoryWindow(List<ConversationMessage> rows,
                                              String previousSummary) {
        List<Map<String, Object>> allMessages = rows.stream()
                .map(row -> historyMessage(row, 600)).toList();
        int allTokens = estimateTokens(previousSummary, List.of(), allMessages);
        if (rows.size() <= MAX_RECENT_MESSAGE_COUNT
                && allTokens <= HISTORY_CONTEXT_HIGH_WATERMARK) {
            return new HistoryWindow(List.of(), rows);
        }

        List<ConversationMessage> recentRows = new ArrayList<>();
        int index = rows.size();
        while (index > 0 && recentRows.size() < MAX_RECENT_MESSAGE_COUNT) {
            int start = index - 1;
            // Keep a complete user/assistant turn together whenever the pair
            // is present; never leave an assistant answer without its question.
            if (start > 0 && "ASSISTANT".equalsIgnoreCase(rows.get(start).getRole())
                    && "USER".equalsIgnoreCase(rows.get(start - 1).getRole())) {
                start--;
            }
            List<ConversationMessage> candidateRows = new ArrayList<>(
                    rows.subList(start, index));
            candidateRows.addAll(recentRows);
            List<Map<String, Object>> candidateRecent = candidateRows.stream()
                    .map(row -> historyMessage(row, 600)).toList();
            int candidateTokens = estimateTokens(previousSummary, List.of(), candidateRecent);
            if (!recentRows.isEmpty()
                    && candidateTokens > HISTORY_CONTEXT_TARGET_TOKEN_LIMIT) {
                break;
            }
            recentRows = candidateRows;
            index = start;
        }
        int compactCount = rows.size() - recentRows.size();
        List<ConversationMessage> compactRows = rows.subList(0, compactCount);
        return new HistoryWindow(compactRows, recentRows);
    }

    /** Rebuild the prompt-ready short memory after a completed turn commits. */
    public void refreshHistoryCache(String conversationId) {
        try {
            historyCache().fastRemove(conversationId);
            loadHistoryContext(conversationId, null, false);
        } catch (Exception e) {
            log.debug("Copilot history cache refresh skipped {}: {}",
                    conversationId, e.getMessage());
        }
    }

    private Map<String, Object> historyMessage(ConversationMessage row, int limit) {
        Map<String, Object> view = new LinkedHashMap<>();
        view.put("id", row.getId());
        view.put("role", row.getRole());
        view.put("intent", row.getIntentType());
        view.put("content", clipPreservingHeadTail(row.getContent(), limit));
        return view;
    }

    private void persistHistorySnapshot(String conversationId,
                                        HistoryContext history,
                                        String updatedSummary) {
        if (!StringUtils.hasText(updatedSummary)
                || history.messagesToCompact().isEmpty()
                || history.sourceMessageEndId() == null) {
            return;
        }
        try {
            ContextSnapshotRow row = new ContextSnapshotRow();
            row.setRunId(clip("copilot:" + conversationId, 64));
            row.setConversationId(conversationId);
            row.setSummaryVersion(history.summaryVersion() + 1);
            row.setSourceMessageStartId(history.sourceMessageStartId());
            row.setSourceMessageEndId(history.sourceMessageEndId());
            row.setFirstKeptMessageId(history.firstKeptMessageId());
            row.setBeforeTokenEstimate(history.beforeTokenEstimate());
            row.setAfterTokenEstimate(Math.max(1,
                    (updatedSummary.length() + history.recentMessages().stream()
                            .mapToInt(item -> String.valueOf(
                                    item.getOrDefault("content", "")).length())
                            .sum()) / 2));
            row.setReason("copilot_token_budget_2400_target_1600");
            row.setSummary(clipPreservingHeadTail(updatedSummary, 1600));
            row.setCreateTime(LocalDateTime.now());
            contextSnapshotMapper.insert(row);
        } catch (Exception e) {
            log.info("Copilot context snapshot persistence skipped: {}", e.getMessage());
        }
    }

    private int estimateTokens(String summary,
                               List<Map<String, Object>> compact,
                               List<Map<String, Object>> recent) {
        int chars = summary == null ? 0 : summary.length();
        for (Map<String, Object> item : compact) {
            chars += String.valueOf(item.getOrDefault("content", "")).length();
        }
        for (Map<String, Object> item : recent) {
            chars += String.valueOf(item.getOrDefault("content", "")).length();
        }
        return Math.max(1, chars / 2);
    }

    private List<String> defaultSuggestions() {
        return List.of(
                "为什么给这个分数？",
                "主要风险是什么？",
                "改为目标岗位后重新评估");
    }

    private boolean isMcpQuestion(String lower) {
        return containsAny(lower, "mcp", "tools/list", "tools/call", "tool schema",
                "工具描述", "工具参数", "模型选工具", "调用工具");
    }

    private boolean isCheckpointQuestion(String lower) {
        return containsAny(lower, "checkpoint", "check point", "检查点", "断点续跑",
                "暂停后", "恢复后", "如何暂停", "怎么暂停", "暂停和恢复", "暂停/恢复",
                "pause/resume", "resume run", "恢复运行");
    }

    private boolean isRevisionQuestion(String lower) {
        return containsAny(lower, "revision", "修改jd", "修改 jd", "更新jd", "更新 jd",
                "jd 变更", "评估重点", "重点变更", "废弃旧结果", "已取代",
                "受影响节点", "重跑哪些");
    }

    private boolean isEvidenceGapQuestion(String lower) {
        return containsAny(lower, "证据不足", "证据不够", "缺少证据", "没有证据",
                "证据缺口", "信息不足", "材料不足", "无法验证", "不能验证",
                "未验证", "insufficient evidence", "unverified");
    }

    private boolean isRetrievalQuestion(String lower, TurnDecision decision) {
        return "EVIDENCE_QUERY".equalsIgnoreCase(decision.intent())
                || containsAny(lower, "rag", "检索", "召回", "重排", "rerank",
                        "query rewrite", "向量搜索", "向量检索", "知识库");
    }

    private boolean isCandidateResultQuestion(String lower) {
        return containsAny(lower, "结论", "分数", "推荐", "怎么样", "候选人", "评估");
    }

    private boolean containsAny(String value, String... terms) {
        for (String term : terms) {
            if (value.contains(term)) {
                return true;
            }
        }
        return false;
    }

    private static String clip(String value, int limit) {
        if (value == null || value.length() <= limit) {
            return value;
        }
        return value.substring(0, limit);
    }

    /** Preserve both the beginning and terminal status/evidence of long text. */
    private static String clipPreservingHeadTail(String value, int limit) {
        if (value == null || value.length() <= limit) {
            return value;
        }
        String marker = "\n[…中间内容已截断…]\n";
        int available = Math.max(2, limit - marker.length());
        int head = Math.max(1, (int) Math.ceil(available * 0.6));
        int tail = Math.max(1, available - head);
        return value.substring(0, head) + marker
                + value.substring(value.length() - tail);
    }

    @SuppressWarnings("unchecked")
    private String buildRichReportAnswer(Map<String, Object> report, String recLabel) {
        StringBuilder sb = new StringBuilder();
        sb.append("**").append(recLabel).append("**\n\n");
        Object dimsObj = report.get("dimensions");
        if (dimsObj instanceof List<?> dims && !dims.isEmpty()) {
            sb.append("维度评分：");
            for (Object d : dims) {
                if (d instanceof Map<?, ?> dim) {
                    sb.append(dim.get("name")).append(" ")
                      .append(dim.get("score")).append("/100, ");
                }
            }
            sb.setLength(sb.length() - 2);
            sb.append("\n\n");
        }
        Object strengthsObj = report.get("strengths");
        if (strengthsObj instanceof List<?> strengths && !strengths.isEmpty()) {
            sb.append("核心优势: ");
            int c = 0;
            for (Object s : strengths) {
                if (c++ >= 3) break;
                String t = String.valueOf(s);
                sb.append(t.length() > 50 ? t.substring(0, 50) + "..." : t).append("; ");
            }
            sb.append("\n\n");
        }
        Object risksObj = report.get("risks");
        if (risksObj instanceof List<?> risks && !risks.isEmpty()) {
            sb.append("风险(").append(risks.size()).append("项): ");
            int c = 0;
            for (Object r : risks) {
                if (c++ >= 2) break;
                if (r instanceof Map<?, ?> risk) {
                    String claim = String.valueOf(risk.get("claim"));
                    sb.append(claim.length() > 40 ? claim.substring(0, 40) + "..." : claim)
                      .append("(").append(risk.get("severity")).append("); ");
                }
            }
            sb.append("\n\n");
        }
        Object probesObj = report.get("interviewProbes");
        if (probesObj instanceof List<?> probes && !probes.isEmpty()) {
            sb.append("面试追问(").append(probes.size()).append("): ");
            int c = 0;
            for (Object q : probes) {
                if (c++ >= 2) break;
                if (q instanceof Map<?, ?> probe) {
                    String question = String.valueOf(probe.get("question"));
                    sb.append(question.length() > 50 ? question.substring(0, 50) + "..." : question).append("; ");
                }
            }
            sb.append("\n\n");
        }
        sb.append("完整证据链见决策报告页。");
        return sb.toString();
    }

    private String evaluateArithmetic(Matcher matcher) {
        try {
            double a = Double.parseDouble(matcher.group(1));
            double b = Double.parseDouble(matcher.group(3));
            String op = matcher.group(2);
            double result = switch (op) {
                case "+" -> a + b;
                case "-" -> a - b;
                case "*", "x", "×" -> a * b;
                case "/" -> b == 0 ? Double.NaN : a / b;
                default -> Double.NaN;
            };
            if (Double.isNaN(result)) {
                return "无法计算该表达式。";
            }
            if (Math.rint(result) == result) {
                return String.valueOf((long) result);
            }
            return String.valueOf(result);
        } catch (Exception e) {
            return "无法计算该表达式。";
        }
    }

    private List<Map<String, Object>> toRefMaps(List<ContextRefRequest> refs) {
        if (refs == null || refs.isEmpty()) {
            return List.of();
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (ContextRefRequest ref : refs) {
            if (ref == null) {
                continue;
            }
            Map<String, Object> map = new LinkedHashMap<>();
            map.put("type", ref.type());
            map.put("id", ref.id());
            if (ref.revision() != null) {
                map.put("revision", ref.revision());
            }
            if (ref.version() != null) {
                map.put("version", ref.version());
            }
            out.add(map);
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> mapList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object item : list) {
            if (item instanceof Map<?, ?> map) {
                out.add(new LinkedHashMap<>((Map<String, Object>) map));
            }
        }
        return out;
    }

    private List<String> stringList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        List<String> out = new ArrayList<>();
        for (Object item : list) {
            if (item != null && StringUtils.hasText(String.valueOf(item))) {
                out.add(String.valueOf(item));
            }
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> getLatestStructuredReport(String conversationId) {
        try {
            AgentRun run = agentRunMapper.selectOne(
                    new LambdaQueryWrapper<AgentRun>()
                            .eq(AgentRun::getConversationId, conversationId)
                            .eq(AgentRun::getStatus, "SUCCEEDED")
                            .isNotNull(AgentRun::getSharedState)
                            .orderByDesc(AgentRun::getFinishedAt)
                            .last("LIMIT 1"));
            if (run == null || !StringUtils.hasText(run.getSharedState())) {
                return Map.of();
            }
            Map<String, Object> state = objectMapper.readValue(
                    run.getSharedState(), new TypeReference<>() {});
            Object artifacts = state.get("artifacts");
            if (artifacts instanceof Map<?, ?> arts) {
                Object report = arts.get("finalReport");
                if (report instanceof Map<?, ?> reportMap) {
                    return new LinkedHashMap<>((Map<String, Object>) reportMap);
                }
            }
            return Map.of();
        } catch (Exception e) {
            log.debug("getLatestStructuredReport failed: {}", e.getMessage());
            return Map.of();
        }
    }
}
