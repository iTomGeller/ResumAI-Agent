package com.resumai.agent.domain.enums;

/**
 * HR 或面试官对 AI 评估报告的反馈类型。
 */
public enum FeedbackType {

    /**
     * 点赞反馈。
     */
    LIKE,

    /**
     * 点踩反馈。
     */
    DISLIKE,

    /**
     * 普通文字评论。
     */
    COMMENT,

    /**
     * 对 AI 结论进行修正。
     */
    CORRECTION,

    /**
     * 标记候选人风险。
     */
    RISK_MARK,

    /**
     * 人工覆盖 AI 推荐结论。
     */
    RECOMMENDATION_OVERRIDE
}
