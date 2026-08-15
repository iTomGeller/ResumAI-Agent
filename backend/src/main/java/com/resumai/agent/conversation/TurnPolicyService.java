package com.resumai.agent.conversation;

import com.resumai.agent.domain.entity.AgentRun;
import com.resumai.agent.domain.entity.ConversationSession;
import com.resumai.agent.domain.enums.RunStatus;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Deterministic turn router. Priority:
 * explicit stop → CONTROL;
 * job/goal change → SUPERSEDE_RUN or CREATE_REVISION;
 * add fact → MERGE_CONTEXT or CREATE_REVISION;
 * needs evidence → BACKGROUND_QUERY;
 * question/chat → DIRECT_REPLY.
 */
@Service
public class TurnPolicyService {

    public TurnDecision decide(ConversationSession session, AgentRun activeRun, String content) {
        return decide(session, activeRun, null, content);
    }

    public TurnDecision decide(ConversationSession session, AgentRun activeRun,
                               AgentRun pendingRun, String content) {
        String text = StringUtils.hasText(content) ? content.trim() : "";
        String lower = text.toLowerCase(Locale.ROOT);

        if (!StringUtils.hasText(text)) {
            return new TurnDecision(TurnDisposition.DIRECT_REPLY, "CLARIFY", List.of(),
                    null, 1.0, "empty_turn", true);
        }

        if (explicitStop(lower)) {
            return new TurnDecision(TurnDisposition.CONTROL, "CONTROL_COMMAND", List.of(),
                    "CANCEL", 0.99, "explicit_stop", false);
        }
        if (explicitPause(lower)) {
            return new TurnDecision(TurnDisposition.CONTROL, "CONTROL_COMMAND", List.of(),
                    "PAUSE", 0.99, "explicit_pause", false);
        }
        if (explicitResume(lower) && !isQuestionOrChat(lower)) {
            return new TurnDecision(TurnDisposition.CONTROL, "CONTROL_COMMAND", List.of(),
                    "RESUME", 0.99, "explicit_resume", false);
        }

        if (isAmbiguousGoalChange(lower)) {
            return new TurnDecision(TurnDisposition.DIRECT_REPLY, "CLARIFY_GOAL_CHANGE",
                    List.of(), null, 0.78, "ambiguous_goal_change", true);
        }

        if (changesJobOrGoal(lower)) {
            if (activeRun != null && isActive(activeRun)) {
                return new TurnDecision(TurnDisposition.SUPERSEDE_RUN, "GOAL_CHANGE",
                        List.of("jd_requirements", "technical_findings", "final_report"),
                        null, 0.94, "job_or_goal_change_supersede", false);
            }
            return new TurnDecision(TurnDisposition.CREATE_REVISION, "GOAL_CHANGE",
                    List.of("jd_requirements", "technical_findings", "final_report"),
                    null, 0.94, "job_or_goal_change_revision", false);
        }

        if (addsCandidateFact(lower)) {
            AgentRun mergeTarget = pendingRun != null ? pendingRun : activeRun;
            if (canMergeBeforeConsumption(mergeTarget)) {
                return new TurnDecision(TurnDisposition.MERGE_CONTEXT, "CONTEXT_ADD",
                        List.of(), null, 0.92, "merge_fact_into_pending", false);
            }
            if (activeRun != null && isActive(activeRun)) {
                return new TurnDecision(TurnDisposition.SUPERSEDE_RUN, "CONTEXT_ADD",
                        List.of("resume_facts", "technical_findings", "final_report"),
                        null, 0.92, "fact_requires_active_revision_supersede", false);
            }
            return new TurnDecision(TurnDisposition.CREATE_REVISION, "CONTEXT_ADD",
                    List.of("resume_facts", "technical_findings", "final_report"),
                    null, 0.92, "fact_requires_revision", false);
        }

        if (explicitEvaluationRequest(lower)) {
            if (activeRun != null && isActive(activeRun)) {
                return new TurnDecision(TurnDisposition.SUPERSEDE_RUN, "EVALUATION_REQUEST",
                        List.of("final_report"), null, 0.9, "explicit_evaluation_supersede", false);
            }
            return new TurnDecision(TurnDisposition.CREATE_REVISION, "EVALUATION_REQUEST",
                    List.of("final_report"), null, 0.9, "explicit_evaluation_request", false);
        }

        if (needsEvidenceTool(lower)) {
            return new TurnDecision(TurnDisposition.BACKGROUND_QUERY, "EVIDENCE_QUERY",
                    List.of(), null, 0.88, "needs_evidence_tool", false);
        }

        if (isQuestionOrChat(lower) || isArithmeticOrChat(lower)) {
            return new TurnDecision(TurnDisposition.DIRECT_REPLY, "SIDE_QUESTION",
                    List.of(), null, 0.9, "question_or_chat", false);
        }

        // Default: never invent an evaluation run for unclassified chatter.
        return new TurnDecision(TurnDisposition.DIRECT_REPLY, "SIDE_QUESTION",
                List.of(), null, 0.62, "safe_direct_reply_fallback", false);
    }

    private boolean isActive(AgentRun run) {
        if (run == null || !StringUtils.hasText(run.getStatus())) {
            return false;
        }
        String status = run.getStatus();
        return !Set.of(
                RunStatus.SUCCEEDED.name(), RunStatus.PARTIAL_SUCCESS.name(),
                RunStatus.FAILED.name(), RunStatus.CANCELLED.name(),
                RunStatus.TIMED_OUT.name()
        ).contains(status);
    }

    /**
     * A fact can merge only into a not-yet-started queued run (unconsumed).
     * Active/running work already consumed its prompt — needs a revision.
     */
    boolean canMergeBeforeConsumption(AgentRun activeOrPending) {
        return activeOrPending != null
                && RunStatus.QUEUED.name().equals(activeOrPending.getStatus());
    }

    private boolean explicitStop(String lower) {
        if (matchesAny(lower, "不要取消", "别取消", "不用取消", "先不取消",
                "不要停止", "别停止", "不用停止", "先不停止",
                "don't cancel", "do not cancel", "don't stop", "do not stop")) {
            return false;
        }
        return matchesAny(lower, "取消", "停止", "别跑了", "终止", "停止生成", "cancel", "stop");
    }

    private boolean explicitPause(String lower) {
        if (matchesAny(lower, "不要暂停", "别暂停", "不用暂停", "先不暂停", "别停", "不要停",
                "don't pause", "do not pause")) {
            return false;
        }
        return matchesAny(lower, "暂停", "先停一下", "pause");
    }

    private boolean explicitResume(String lower) {
        // Bare English "resume" is ambiguous with the résumé document, so
        // require an explicit run/evaluation object. Chinese control verbs are
        // unambiguous in this product context.
        return matchesAny(lower, "继续", "恢复", "接着跑",
                "resume run", "resume this run", "resume the run",
                "resume evaluation", "resume assessment",
                "continue run", "continue this run", "continue the run",
                "continue evaluation", "continue assessment");
    }

    private boolean changesJobOrGoal(String lower) {
        return matchesAny(lower, "换jd", "修改jd", "更新jd", "新jd", "岗位改成", "改投",
                "目标岗位", "职位改成", "重点看", "重点评估", "更关注", "忽略学历",
                "不要看学历", "调整权重", "评估重点")
                || (lower.contains("重新评估") && matchesAny(lower, "岗位", "jd", "职位"))
                || (lower.contains("重新") && lower.contains("重点"));
    }

    private boolean addsCandidateFact(String lower) {
        return matchesAny(lower, "补充", "漏了", "还有一段", "新增经历", "更正简历",
                "简历改了", "项目经验", "还有 kafka", "还有kafka")
                && !matchesAny(lower, "备注", "提醒", "面试时间");
    }

    private boolean explicitEvaluationRequest(String lower) {
        return matchesAny(lower, "完整评估", "全面评估", "整体评估", "全面分析",
                "评估这份简历", "综合评估", "重新分析", "发起评估", "开始评估",
                "full evaluation", "evaluate this", "start evaluation", "run evaluation",
                "complete assessment", "comprehensive evaluation");
    }

    private boolean needsEvidenceTool(String lower) {
        return matchesAny(lower, "查一下", "检索", "证据在哪", "原文哪一行", "知识库",
                "搜一下", "引用依据", "出处", "来源是什么",
                "context7", "官方文档", "最新文档", "最新api", "最新 api",
                "library docs", "api docs");
    }

    private boolean isQuestionOrChat(String lower) {
        return lower.contains("?") || lower.contains("？")
                || matchesAny(lower, "为什么", "解释", "什么意思", "现在到哪", "进度",
                "帮我改写", "润色", "模拟面试", "面试题", "比较岗位", "职业规划",
                "投递建议", "顺便问", "另外一个问题", "先问个", "怎么看", "告诉我",
                "结论", "分数", "推荐吗");
    }

    private boolean isArithmeticOrChat(String lower) {
        if (lower.matches("^[\\d\\s\\+\\-\\*/\\(\\)=\\.x×÷]+$")) {
            return true;
        }
        return matchesAny(lower, "你好", "谢谢", "在吗", "帮个忙", "随便聊聊");
    }

    private boolean isAmbiguousGoalChange(String lower) {
        boolean tentative = matchesAny(lower, "也看看", "顺便看看", "要不要看", "也评估", "顺便评估");
        boolean target = matchesAny(lower, "岗", "方向", "jd", "职位", "前端", "后端",
                "全栈", "算法", "数据", "测试", "运维", "产品", "java", "python", "ai");
        return tentative && target;
    }

    private boolean matchesAny(String value, String... terms) {
        for (String term : terms) {
            if (value.contains(term)) {
                return true;
            }
        }
        return false;
    }
}
