package com.resumai.agent.util;

/**
 * 当前请求 HR 上下文（来自 X-HR-Id 请求头）。
 */
public final class HrContext {

    private static final ThreadLocal<String> HR_ID = new ThreadLocal<>();

    private HrContext() {
    }

    public static void setHrId(String hrId) {
        HR_ID.set(hrId);
    }

    public static String getHrId() {
        String hrId = HR_ID.get();
        return hrId != null && !hrId.isBlank() ? hrId : "demo-hr";
    }

    public static void clear() {
        HR_ID.remove();
    }
}
