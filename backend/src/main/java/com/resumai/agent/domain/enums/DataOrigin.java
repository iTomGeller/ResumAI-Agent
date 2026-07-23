package com.resumai.agent.domain.enums;

/** 简历/候选人数据来源；HR KPI 只统计 USER_UPLOAD。 */
public enum DataOrigin {
    USER_UPLOAD,
    BENCHMARK,
    ACCEPTANCE,
    SYSTEM;

    public boolean isHrCohort() {
        return this == USER_UPLOAD;
    }
}
