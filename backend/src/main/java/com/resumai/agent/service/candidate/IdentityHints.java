package com.resumai.agent.service.candidate;

/**
 * 从简历正文/文件名提取的身份启发结果。
 *
 * @param identityConfidence 0–1；EMAIL/PHONE 高，NAME 中，LEGACY_UNVERIFIED 低
 */
public record IdentityHints(
        String displayName,
        String email,
        String phone,
        String identityKey,
        String identitySource,
        String resumeFingerprint,
        double identityConfidence,
        boolean needsMergeReview
) {
}
