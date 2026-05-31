package com.resumai.agent.util;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import org.springframework.util.StringUtils;

/**
 * Markdown 章节解析与纯文本清洗工具。
 */
public final class MarkdownTextUtil {

    private static final Pattern HEADING = Pattern.compile("^#{1,6}\\s+.+");
    private static final Pattern BOLD_NUMBERED_SECTION = Pattern.compile("^\\*\\*\\d+\\.\\s*.+\\*\\*.*");
    private static final Pattern HASH_BOLD_NUMBERED = Pattern.compile("^#+\\s+\\*\\*\\d+\\.\\s*.+\\*\\*.*");
    private static final Pattern LIST_ITEM = Pattern.compile("^[-*•]\\s+(.+)$");
    private static final Pattern NUMBERED_LIST = Pattern.compile("^\\d+[.)]\\s+(.+)$");
    private static final Pattern QUESTION_SHAPE = Pattern.compile(".*([?？]|请说明|请描述|请举例|如何|为什么|怎样|是否|能否).*");

    /** 高置信面试追问章节标题关键词（禁止单独使用「面试」「问题」等宽泛词）。 */
    private static final List<String> INTERVIEW_SECTION_KEYWORDS = List.of(
            "面试追问", "追问建议", "面试问题", "核心追问", "追问清单"
    );

    private MarkdownTextUtil() {
    }

    public static boolean isMarkdownHeading(String line) {
        if (!StringUtils.hasText(line)) {
            return false;
        }
        String trimmed = line.trim();
        if (HEADING.matcher(trimmed).matches()) {
            return true;
        }
        if (BOLD_NUMBERED_SECTION.matcher(trimmed).matches()) {
            return true;
        }
        return HASH_BOLD_NUMBERED.matcher(trimmed).matches();
    }

    private static boolean headingMatchesSection(String heading, List<String> sectionNames) {
        String normalized = heading.replace("*", "").trim();
        for (String keyword : sectionNames) {
            if (normalized.contains(keyword)) {
                return true;
            }
        }
        return false;
    }

    public static String findMarkdownSection(String text, List<String> sectionNames) {
        if (!StringUtils.hasText(text)) {
            return "";
        }
        String[] lines = text.split("\\R");
        StringBuilder sb = new StringBuilder();
        boolean inSection = false;

        for (String line : lines) {
            String trimmed = line.trim();
            if (isMarkdownHeading(trimmed)) {
                if (inSection) {
                    break;
                }
                if (headingMatchesSection(trimmed, sectionNames)) {
                    inSection = true;
                }
                continue;
            }
            if (inSection) {
                sb.append(line).append('\n');
            }
        }
        return sb.toString();
    }

    public static List<String> extractSectionItems(String text, List<String> sectionNames) {
        String sectionContent = findMarkdownSection(text, sectionNames);
        List<String> items = new ArrayList<>();
        if (!StringUtils.hasText(sectionContent)) {
            return items;
        }
        for (String line : sectionContent.split("\\R")) {
            String trimmed = line.trim();
            if (!StringUtils.hasText(trimmed) || isMarkdownHeading(trimmed)) {
                continue;
            }
            String item = null;
            var bullet = LIST_ITEM.matcher(trimmed);
            var numbered = NUMBERED_LIST.matcher(trimmed);
            if (bullet.matches()) {
                item = bullet.group(1);
            } else if (numbered.matches()) {
                item = numbered.group(1);
            } else if (trimmed.startsWith(">")) {
                item = trimmed.substring(1).trim();
            }
            if (StringUtils.hasText(item)) {
                item = stripMarkdown(item);
                if (item.length() > 4) {
                    items.add(item);
                }
            }
        }
        return items.stream().limit(8).toList();
    }

    /**
     * 从评估报告中提取面试追问，过滤推荐理由误匹配。
     */
    public static List<String> extractInterviewQuestions(String text) {
        List<String> raw = extractSectionItems(text, INTERVIEW_SECTION_KEYWORDS);
        List<String> filtered = new ArrayList<>();
        for (String item : raw) {
            if (isLikelyInterviewQuestion(item)) {
                filtered.add(item);
            }
        }
        return filtered.stream().limit(8).toList();
    }

    static boolean isLikelyInterviewQuestion(String text) {
        if (!StringUtils.hasText(text)) {
            return false;
        }
        String normalized = text.trim();
        if (normalized.contains("强相关性") || normalized.contains("潜力突出") || normalized.contains("风险可控")) {
            return false;
        }
        if (normalized.contains("建议优先面试") || normalized.contains("强烈推荐") || normalized.contains("推荐结论")) {
            return false;
        }
        return QUESTION_SHAPE.matcher(normalized).matches();
    }

    public static String stripMarkdown(String text) {
        if (!StringUtils.hasText(text)) {
            return "";
        }
        return text
                .replaceAll("`+", "")
                .replaceAll("\\*\\*(.+?)\\*\\*", "$1")
                .replaceAll("\\*(.+?)\\*", "$1")
                .replaceAll("^#{1,6}\\s+", "")
                .replaceAll("^[-*•]\\s+", "")
                .replaceAll("^\\d+[.)]\\s+", "")
                .replaceAll("\\[(.+?)]\\(.+?\\)", "$1")
                .trim();
    }

    public static List<String> stripMarkdownList(List<String> items) {
        if (items == null || items.isEmpty()) {
            return List.of();
        }
        return items.stream().map(MarkdownTextUtil::stripMarkdown).filter(StringUtils::hasText).toList();
    }
}
