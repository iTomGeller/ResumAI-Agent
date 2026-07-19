package com.resumai.agent.service.run;

import java.util.Locale;
import org.springframework.stereotype.Component;

/**
 * Rule-first task-category classifier for policy bucketing. The Python
 * coordinator refines routing with model assistance; this deterministic
 * category is what policy statistics are aggregated on, so it must be stable
 * and cheap.
 */
@Component
public class RunTypeClassifier {

    public String classify(String message, String jobCategory) {
        String text = message == null ? "" : message.toLowerCase(Locale.ROOT);
        if (containsAny(text, "面试题", "追问", "面试问题", "问什么")) {
            return "interview_questions";
        }
        if (containsAny(text, "改写", "重写", "润色", "优化描述", "怎么写")) {
            if (containsAny(text, "整体", "全部", "整份", "简历优化")) {
                return "resume_optimize";
            }
            return "project_rewrite";
        }
        if (containsAny(text, "整体优化", "简历优化", "优化简历")) {
            return "resume_optimize";
        }
        if (containsAny(text, "时间线", "时间重叠", "空窗", "履历时间")) {
            return "timeline_check";
        }
        if (containsAny(text, "风险", "夸大", "造假", "可信")) {
            return "risk_check";
        }
        if (containsAny(text, "证据", "核验", "佐证", "验证")) {
            return "evidence_check";
        }
        if (containsAny(text, "缺口", "差距", "还差", "gap", "缺少哪些")) {
            return "jd_gap";
        }
        if (containsAny(text, "技术栈", "技术匹配", "技能匹配", "会不会", "掌握")) {
            return "tech_match";
        }
        if (containsAny(text, "项目经历", "项目分析", "项目深度", "项目含金量")) {
            return "project_analysis";
        }
        if (containsAny(text, "java后端", "java 后端", "后端岗", "后端工程师")) {
            return "backend_eval";
        }
        if (containsAny(text, "agent岗", "agent 岗", "ai agent", "大模型岗", "llm岗", "llm 岗")) {
            return "agent_eval";
        }
        if (containsAny(text, "完整评估", "全面评估", "整体评估", "全面分析", "评估这份简历", "综合评估")) {
            return "full_evaluation";
        }
        if (containsAny(text, "jd", "岗位描述", "职位描述", "针对这个岗位")) {
            return "jd_evaluation";
        }
        if (containsAny(text, "为什么", "怎么看", "解释", "上一轮", "刚才", "继续")) {
            return "followup";
        }
        if (text.length() > 60 || containsAny(text, "评估", "分析")) {
            return "full_evaluation";
        }
        return "quick_answer";
    }

    /** Categories that go through the heavyweight multi-agent pipeline. */
    public boolean isHeavy(String category) {
        return switch (category) {
            case "quick_answer", "followup" -> false;
            default -> true;
        };
    }

    private boolean containsAny(String text, String... needles) {
        for (String needle : needles) {
            if (text.contains(needle)) {
                return true;
            }
        }
        return false;
    }
}
