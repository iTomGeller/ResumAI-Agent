package com.resumai.agent.domain.enums;

/**
 * Meta-Agent 规则变更审核状态。
 */
public enum ApprovalStatus {

    /**
     * 等待管理员审核。
     */
    PENDING,

    /**
     * 已批准并允许生效。
     */
    APPROVED,

    /**
     * 已拒绝。
     */
    REJECTED,

    /**
     * 低风险变更已自动应用。
     */
    AUTO_APPLIED,

    /**
     * 已回滚。
     */
    ROLLED_BACK
}
