package com.resumai.agent.conversation;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.api.dto.ContextRefRequest;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.dao.AgentRunMapper;
import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.service.run.AgentRuntimeClient;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Short Copilot replies via workflow ConversationalAgent. Never produces
 * StructuredReport / ReportAgent output.
 */
@Service
public class ConversationReplyService {

    private static final Logger log = LoggerFactory.getLogger(ConversationReplyService.class);
    private static final Pattern SIMPLE_ARITH =
            Pattern.compile("^\\s*(\\d+)\\s*([+\\-*/x×])\\s*(\\d+)\\s*$");

    private final AgentRuntimeClient runtimeClient;
    private final AgentRunMapper agentRunMapper;
    private final ObjectMapper objectMapper;

    public ConversationReplyService(AgentRuntimeClient runtimeClient,
                                    AgentRunMapper agentRunMapper,
                                    ObjectMapper objectMapper) {
        this.runtimeClient = runtimeClient;
        this.agentRunMapper = agentRunMapper;
        this.objectMapper = objectMapper;
    }

    public record CopilotReply(
            String turnId,
            String answer,
            List<Map<String, Object>> citations,
            List<Map<String, Object>> actions,
            List<String> suggestions
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
        // Include structuredReport from the latest completed run for rich Copilot answers
        Map<String, Object> report = getLatestStructuredReport(session.getId());
        if (report != null && !report.isEmpty()) {
            snapshot.put("structuredReport", report);
        }
        body.put("contextSnapshot", snapshot);

        Optional<Map<String, Object>> remote = runtimeClient.replyConversation(body);
        if (remote.isPresent()) {
            return fromPayload(turnId, remote.get());
        }
        log.debug("workflow reply unavailable; using local CopilotAnswer fallback");
        return localFallback(turnId, request.content(), decision, session);
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
        return new CopilotReply(remoteTurn, answer, citations, actions, suggestions);
    }

    private CopilotReply localFallback(String turnId, String content, TurnDecision decision,
                                       ConversationSession session) {
        String answer;
        Matcher arith = SIMPLE_ARITH.matcher(content == null ? "" : content.trim());
        if (arith.matches()) {
            answer = evaluateArithmetic(arith);
        } else if (decision.needsConfirmation()) {
            answer = "我不完全确定你的意图：是想补充当前评估的信息，还是提出一个新的目标？"
                    + "如果要改评估方向，请明确说“改为……重新评估”。";
        } else if (content != null
                && (content.contains("结论") || content.contains("分数")
                    || content.contains("推荐") || content.contains("怎么样")
                    || content.contains("候选人") || content.contains("评估"))) {
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
                answer = "当前还没有可用的评估结果。发起完整评估后，可在决策报告页查看证据化结论。";
            }
        } else {
            answer = "已收到。这是对话回复，不会启动完整评估流水线。"
                    + "若要重新评估，请明确说明岗位/事实变更；若要停止任务，请说“停止”。";
        }
        List<String> suggestions = List.of(
                "为什么给这个分数？",
                "主要风险是什么？",
                "改为目标岗位后重新评估");
        return new CopilotReply(turnId, answer, List.of(), List.of(), suggestions);
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
                    sb.append(dim.getOrDefault("name", "?")).append(" ")
                      .append(dim.getOrDefault("score", "?")).append("/100, ");
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
                    String claim = String.valueOf(risk.getOrDefault("claim", ""));
                    sb.append(claim.length() > 40 ? claim.substring(0, 40) + "..." : claim)
                      .append("(").append(risk.getOrDefault("severity", "")).append("); ");
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
                    String question = String.valueOf(probe.getOrDefault("question", ""));
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
