package com.resumai.agent.service.candidate;

import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.DataOrigin;
import java.util.Locale;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/** 将历史/测试流量与真实 HR 上传区分开。 */
@Component
public class OriginClassifier {

    public DataOrigin classify(ResumeTask task) {
        if (task == null) {
            return DataOrigin.SYSTEM;
        }
        return classify(task.getFileName(), task.getUploadedBy(), task.getTraceId());
    }

    public DataOrigin classify(String fileName, String uploadedBy, String traceId) {
        String by = uploadedBy == null ? "" : uploadedBy.trim().toLowerCase(Locale.ROOT);
        if ("bench-runner".equals(by) || "benchmark".equals(by) || "harness".equals(by)) {
            return DataOrigin.BENCHMARK;
        }
        if ("acceptance".equals(by) || "verify".equals(by)) {
            return DataOrigin.ACCEPTANCE;
        }

        String name = fileName == null ? "" : fileName.toLowerCase(Locale.ROOT);
        String tid = traceId == null ? "" : traceId.toLowerCase(Locale.ROOT);

        if (containsAny(name, "accept_", "acceptance", "verify-", "verify_", "gold_case", "benchmark", "bench_")) {
            if (containsAny(name, "accept_", "acceptance", "verify-", "verify_")) {
                return DataOrigin.ACCEPTANCE;
            }
            return DataOrigin.BENCHMARK;
        }
        if (containsAny(tid, "bench-", "benchmark", "accept-", "verify-")) {
            if (containsAny(tid, "accept-", "verify-")) {
                return DataOrigin.ACCEPTANCE;
            }
            return DataOrigin.BENCHMARK;
        }
        if (!StringUtils.hasText(fileName) && !StringUtils.hasText(uploadedBy)) {
            return DataOrigin.SYSTEM;
        }
        return DataOrigin.USER_UPLOAD;
    }

    private static boolean containsAny(String haystack, String... needles) {
        if (!StringUtils.hasText(haystack) || needles == null) {
            return false;
        }
        for (String n : needles) {
            if (n != null && haystack.contains(n)) {
                return true;
            }
        }
        return false;
    }
}
