package com.resumai.agent.service.run;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class RunTypeClassifierTest {

    private final RunTypeClassifier classifier = new RunTypeClassifier();

    @Test
    void mapsBusinessScenariosToCategories() {
        assertEquals("full_evaluation", classifier.classify("请对这份简历做完整评估", null));
        assertEquals("tech_match", classifier.classify("技术栈匹配度怎么样", null));
        assertEquals("project_analysis", classifier.classify("分析一下项目经历的深度", null));
        assertEquals("timeline_check", classifier.classify("检查一下履历时间线有没有问题", null));
        assertEquals("risk_check", classifier.classify("这份简历有没有夸大的风险", null));
        assertEquals("evidence_check", classifier.classify("帮我核验这些结论的证据", null));
        assertEquals("jd_gap", classifier.classify("相对这个JD还差哪些技能，做个缺口分析", null));
        assertEquals("project_rewrite", classifier.classify("帮我改写第一个项目描述", null));
        assertEquals("resume_optimize", classifier.classify("对简历整体优化一下", null));
        assertEquals("interview_questions", classifier.classify("生成几道面试追问", null));
        assertEquals("backend_eval", classifier.classify("按 Java 后端岗重新评估", null));
        assertEquals("agent_eval", classifier.classify("按 AI Agent 岗位重新评估一次", null));
        assertEquals("followup", classifier.classify("为什么这个项目分低？", null));
        assertEquals("quick_answer", classifier.classify("你好", null));
    }

    @Test
    void heavyVsLightRouting() {
        assertTrue(classifier.isHeavy("full_evaluation"));
        assertTrue(classifier.isHeavy("tech_match"));
        assertFalse(classifier.isHeavy("quick_answer"));
        assertFalse(classifier.isHeavy("followup"));
    }
}
