package com.resumai.agent.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class ConversationIntentClassifierTest {

    private final ConversationIntentClassifier classifier = new ConversationIntentClassifier();

    @Test
    void sideQuestionKeepsCurrentEvaluationRunning() {
        var decision = classifier.classify("先解释一下 RAG 命中低是什么意思，再继续跑");

        assertEquals("SIDE_QUESTION", decision.intent());
        assertFalse(decision.affectsEvaluation());
        assertTrue(decision.answerThenResume());
        assertTrue(decision.affectedNodes().isEmpty());
    }

    @Test
    void goalChangeCreatesTargetedRevision() {
        var decision = classifier.classify("目标岗位改成前端，使用这个新 JD 重新评估");

        assertEquals("GOAL_CHANGE", decision.intent());
        assertTrue(decision.affectsEvaluation());
        assertEquals("CREATE_REVISION", decision.action());
        assertFalse(decision.affectedNodes().contains("resume_parse"));
        assertTrue(decision.affectedNodes().contains("jd_match"));
    }

    @Test
    void ambiguousDirectionDoesNotInterruptRun() {
        var decision = classifier.classify("也看看前端吧");

        assertEquals("CLARIFY_GOAL_CHANGE", decision.intent());
        assertFalse(decision.affectsEvaluation());
        assertTrue(decision.needsConfirmation());
    }

    @Test
    void controlCommandsHavePriority() {
        var decision = classifier.classify("先别分析 JD 了，暂停");

        assertEquals("CONTROL_COMMAND", decision.intent());
        assertEquals("PAUSE", decision.action());
    }

    @Test
    void negatedControlDoesNotCancelOrPause() {
        var cancel = classifier.classify("不要取消，我只是想比较另一个岗位");
        var pause = classifier.classify("别暂停，我先问个进度问题");

        assertFalse("CANCEL".equals(cancel.action()));
        assertFalse("PAUSE".equals(pause.action()));
        assertEquals("SIDE_QUESTION", pause.intent());
    }
}
