package com.resumai.agent.util;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import org.junit.jupiter.api.Test;

class MarkdownTextUtilTest {

    @Test
    void extractsNumberedInterviewQuestionsWithoutTruncatingOnListItems() {
        String report = """
                #### **5. 面试追问建议**
                1. 关于经验年限：请说明 3 年经验与 5 年岗位要求的差距如何弥补？
                2. 关于 Java 21 / Spring Boot 3：请描述 PE 管理服务重构中的技术选型。
                3. 关于 RAG 与 Trace：请说明 AgentOps 链路中的可观测方案。
                4. 关于 Docker 部署：请举例一次线上发布与回滚流程。
                #### **6. 其他**
                不应被提取
                """;

        List<String> questions = MarkdownTextUtil.extractInterviewQuestions(report);

        assertEquals(4, questions.size());
        assertTrue(questions.get(0).contains("经验年限"));
        assertTrue(questions.get(1).contains("Java 21"));
        assertTrue(questions.stream().noneMatch(q -> q.contains("不应被提取")));
    }

    @Test
    void numberedListIsNotTreatedAsHeading() {
        assertFalse(MarkdownTextUtil.isMarkdownHeading("1. 关于经验年限"));
        assertTrue(MarkdownTextUtil.isMarkdownHeading("#### **5. 面试追问建议**"));
    }

    @Test
    void stripMarkdownRemovesBoldAndBackticks() {
        assertEquals("经验年限严重不符", MarkdownTextUtil.stripMarkdown("**经验年限严重不符**"));
        assertEquals("Java 21", MarkdownTextUtil.stripMarkdown("`Java 21`"));
    }

    @Test
    void recommendationSectionDoesNotPolluteInterviewQuestions() {
        String report = """
                #### **2. 推荐结论：有条件推荐（建议优先面试）**
                **理由：**
                *   **强相关性：** 候选人在字节跳动的实习经历，直接涉及 AI Agent 平台。
                *   **潜力突出：** 虽然经验年限不足，但技术深度突出。
                *   **风险可控：** 主要风险在于经验年限，需要通过面试重点验证。
                #### **5. 面试追问**
                1. 请详细描述你在字节跳动实习期间主导的最复杂项目？
                2. 请说明 Java 21 虚拟线程在你项目中的潜在应用？
                """;

        List<String> questions = MarkdownTextUtil.extractInterviewQuestions(report);

        assertEquals(2, questions.size());
        assertTrue(questions.stream().noneMatch(q -> q.contains("强相关性")));
        assertTrue(questions.stream().noneMatch(q -> q.contains("潜力突出")));
    }
}
