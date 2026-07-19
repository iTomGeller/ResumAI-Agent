package com.resumai.agent.service;

import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * Deterministic, high-priority routing for conversational control and common
 * evaluation mutations. Ambiguous turns keep the active run alive and ask for
 * confirmation instead of guessing the user's intent.
 */
@Component
public class ConversationIntentClassifier {

    public Decision classify(String rawContent) {
        String content = StringUtils.hasText(rawContent) ? rawContent.trim() : "";
        String lower = content.toLowerCase(Locale.ROOT);

        boolean cancelNegated = matchesAny(lower, "不要取消", "别取消", "不用取消", "先不取消", "don't cancel", "do not cancel");
        boolean pauseNegated = matchesAny(lower, "不要暂停", "别暂停", "不用暂停", "先不暂停", "别停", "不要停", "don't pause", "do not pause");
        if (!cancelNegated && matchesAny(lower, "取消", "停止", "别跑了", "终止", "cancel", "stop")) {
            return control("CANCEL");
        }
        if (!pauseNegated && matchesAny(lower, "暂停", "先停一下", "pause")) {
            return control("PAUSE");
        }
        if (matchesAny(lower, "继续", "恢复", "接着跑", "resume") && !isSideQuest(lower)) {
            return control("RESUME");
        }

        if (isAmbiguousGoalChange(lower)) {
            return new Decision(
                    "CLARIFY_GOAL_CHANGE", false, false, true, "ASK_CONFIRMATION",
                    List.of(), "你是想把当前主评估切换到这个方向，还是只想顺便比较一下？当前评估会继续运行。"
            );
        }

        if (isJobDescriptionChange(lower)) {
            return mutation(
                    "GOAL_CHANGE", "CREATE_REVISION",
                    List.of("intent", "jd_match", "knowledge_context", "tech_eval", "project_eval", "risk_eval", "evidence_fusion", "report"),
                    "岗位或 JD 已变化，将创建新 revision；复用简历解析，只重跑依赖岗位信息的节点。"
            );
        }
        if (isEvaluationFocusChange(lower)) {
            return mutation(
                    "EVALUATION_FOCUS_CHANGE", "CREATE_REVISION",
                    List.of("knowledge_context", "tech_eval", "project_eval", "risk_eval", "evidence_fusion", "report"),
                    "评估重点已变化，将创建新 revision，并保留不受影响的解析结果。"
            );
        }
        if (isCandidateFactAddition(lower)) {
            return mutation(
                    "CONTEXT_ADD", "CREATE_REVISION",
                    List.of("intent", "resume_parse", "jd_match", "knowledge_context", "tech_eval", "project_eval", "risk_eval", "evidence_fusion", "report"),
                    "你补充了候选人事实，将创建新 revision 并重新核验相关结论。"
            );
        }
        if (isSideQuest(lower)) {
            return new Decision(
                    "SIDE_QUESTION", false, true, false, "ANSWER_AND_CONTINUE",
                    List.of(), "这个想法会作为独立对话任务处理，不会打断当前评估。"
            );
        }
        if (matchesAny(lower, "备注", "记一下", "提醒我", "约面", "面试时间")) {
            return new Decision(
                    "CONTEXT_NOTE", false, false, false, "STORE_NOTE",
                    List.of(), "已记入会话备注，不改变当前评估。"
            );
        }
        return new Decision(
                "SIDE_QUESTION", false, true, false, "ANSWER_AND_CONTINUE",
                List.of(), "我会先回应这个新想法，当前评估继续运行；如果你要改主目标，请明确说“改为……重新评估”。"
        );
    }

    private Decision control(String action) {
        return new Decision(
                "CONTROL_COMMAND", false, false, false, action,
                List.of(), switch (action) {
                    case "PAUSE" -> "将在当前安全节点结束后暂停并保存 checkpoint。";
                    case "RESUME" -> "将从同一 revision 的 checkpoint 继续。";
                    default -> "将立即取消当前运行，迟到结果不会覆盖有效 revision。";
                }
        );
    }

    private Decision mutation(String intent, String action, List<String> affectedNodes, String message) {
        return new Decision(intent, true, false, false, action, affectedNodes, message);
    }

    private boolean isAmbiguousGoalChange(String value) {
        boolean tentativeSwitch = matchesAny(value, "也看看", "顺便看看", "要不要看", "也评估", "顺便评估");
        boolean targetSignal = matchesAny(
                value,
                "岗", "方向", "jd", "职位",
                "前端", "后端", "全栈", "客户端", "算法", "数据", "测试", "运维", "产品", "设计",
                "java", "golang", "python", "大模型", "ai"
        );
        return tentativeSwitch && targetSignal;
    }

    private boolean isJobDescriptionChange(String value) {
        return matchesAny(value, "换jd", "修改jd", "更新jd", "新jd", "岗位改成", "改投", "目标岗位", "职位改成")
                || (value.contains("重新评估") && matchesAny(value, "岗位", "jd", "职位"));
    }

    private boolean isEvaluationFocusChange(String value) {
        return matchesAny(value, "重点看", "重点评估", "更关注", "忽略学历", "不要看学历", "调整权重", "评估重点", "只看")
                || (value.contains("重新") && value.contains("重点"));
    }

    private boolean isCandidateFactAddition(String value) {
        return matchesAny(value, "补充", "漏了", "还有一段", "新增经历", "更正简历", "简历改了", "项目经验")
                && !matchesAny(value, "备注", "提醒", "面试时间");
    }

    private boolean isSideQuest(String value) {
        return matchesAny(value,
                "为什么", "解释", "什么意思", "现在到哪", "进度", "帮我改写", "润色", "模拟面试",
                "面试题", "比较岗位", "职业规划", "投递建议", "顺便问", "另外一个问题", "先问个");
    }

    private boolean matchesAny(String value, String... terms) {
        Set<String> unique = Set.of(terms);
        return unique.stream().anyMatch(value::contains);
    }

    public record Decision(
            String intent,
            boolean affectsEvaluation,
            boolean answerThenResume,
            boolean needsConfirmation,
            String action,
            List<String> affectedNodes,
            String defaultMessage
    ) {
    }
}
