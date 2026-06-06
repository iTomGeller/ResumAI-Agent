package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.service.ResumeRagService;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class RiskTools {

    private final ResumeRagService resumeRagService;
    private final ObjectMapper objectMapper;

    public RiskTools(ResumeRagService resumeRagService, ObjectMapper objectMapper) {
        this.resumeRagService = resumeRagService;
        this.objectMapper = objectMapper;
    }

    @Tool("检索简历证据片段用于交叉验证风险，如频繁跳槽、经历空白、数据夸大等")
    public String milvus_resume_search(
            @P("风险验证查询，如工作经历时间线、项目成果数据等") String query,
            @P("返回结果数量") int topK) {
        try {
            var chunks = resumeRagService.retrieve(query, topK);
            return objectMapper.writeValueAsString(chunks);
        } catch (Exception e) {
            return "{\"error\": \"Risk evidence search failed: " + e.getMessage() + "\"}";
        }
    }

    @Tool("验证简历中的时间线一致性，检测经历重叠、空白期和时间矛盾")
    public String timeline_validator(
            @P("简历全文或包含时间线信息的文本段落") String resumeText) {
        try {
            List<Map<String, Object>> entries = new ArrayList<>();
            Pattern pattern = Pattern.compile("(\\d{4})[-.~年/](\\d{1,2})?\\s*[~—–-]+\\s*(\\d{4}|至今)?[-.~年/]?(\\d{1,2})?");
            Matcher matcher = pattern.matcher(resumeText);

            while (matcher.find()) {
                Map<String, Object> entry = new LinkedHashMap<>();
                int startYear = Integer.parseInt(matcher.group(1));
                int startMonth = matcher.group(2) != null ? Integer.parseInt(matcher.group(2)) : 1;
                int endYear = matcher.group(3) != null && !matcher.group(3).equals("至今")
                        ? Integer.parseInt(matcher.group(3)) : 2026;
                int endMonth = matcher.group(4) != null ? Integer.parseInt(matcher.group(4)) : 12;

                entry.put("start", startYear + "-" + String.format("%02d", startMonth));
                entry.put("end", endYear + "-" + String.format("%02d", endMonth));
                entry.put("durationMonths", (endYear - startYear) * 12 + (endMonth - startMonth));
                entries.add(entry);
            }

            Map<String, Object> result = new LinkedHashMap<>();
            result.put("timelineEntries", entries);
            result.put("totalEntries", entries.size());

            List<String> issues = new ArrayList<>();
            for (int i = 0; i < entries.size() - 1; i++) {
                Map<String, Object> current = entries.get(i);
                Map<String, Object> next = entries.get(i + 1);
                int curDuration = (int) current.get("durationMonths");
                if (curDuration < 0) issues.add("时间倒挂: " + current.get("start") + " ~ " + current.get("end"));
                if (curDuration > 48) issues.add("单段经历超4年: " + current.get("start") + " ~ " + current.get("end"));
            }

            boolean hasGap = false;
            for (int i = 0; i < entries.size() - 1; i++) {
                String currentEnd = (String) entries.get(i).get("end");
                String nextStart = (String) entries.get(i + 1).get("start");
                if (currentEnd.compareTo(nextStart) < 0) {
                    hasGap = true;
                    issues.add("存在空白期: " + currentEnd + " 到 " + nextStart);
                }
            }

            result.put("issues", issues);
            result.put("hasOverlap", entries.stream().anyMatch(e -> (int) e.get("durationMonths") < 0));
            result.put("hasGap", hasGap);
            result.put("riskFlag", !issues.isEmpty());

            return objectMapper.writeValueAsString(result);
        } catch (Exception e) {
            return "{\"error\": \"Timeline validation failed: " + e.getMessage() + "\"}";
        }
    }
}
