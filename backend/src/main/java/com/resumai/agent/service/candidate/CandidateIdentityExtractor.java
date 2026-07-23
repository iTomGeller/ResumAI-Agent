package com.resumai.agent.service.candidate;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Locale;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * 候选人身份提取：邮箱/电话优先，拒绝章节标题当姓名，文件名支持「黄义健的简历 (4).pdf」。
 * 空正文不得共用 sha256("")，改用 legacy:trace:{traceId}。
 */
@Component
public class CandidateIdentityExtractor {

    private static final Set<String> RESERVED_HEADINGS = Set.of(
            "基本信息", "个人信息", "教育经历", "工作经历", "项目经历",
            "技能", "专业技能", "联系方式", "求职意向", "自我评价",
            "个人简历", "简历", "教育背景", "工作经验", "实习经历",
            "荣誉奖项", "证书", "附加信息", "其他信息");

    private static final Pattern EMAIL = Pattern.compile(
            "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}");
    private static final Pattern PHONE = Pattern.compile(
            "(?:\\+?86[-\\s]?)?(1[3-9]\\d{9})|(?:\\+\\d{1,3}[-\\s]?)?\\d{10,14}");
    private static final Pattern NAME_LABEL = Pattern.compile(
            "(?:姓名|名字|Name)\\s*[:：]\\s*([\\u4e00-\\u9fa5A-Za-z·\\s]{2,40})",
            Pattern.CASE_INSENSITIVE);
    /** 黄义健的简历 (4) / 黄义健简历 */
    private static final Pattern FILE_NAME_PERSON = Pattern.compile(
            "^([\\u4e00-\\u9fa5·]{2,8}?)的?简历(?:\\s*[（(]\\d+[）)])?$");

    public IdentityHints extract(String resumeText, String fileName) {
        return extract(resumeText, fileName, null);
    }

    public IdentityHints extract(String resumeText, String fileName, String traceId) {
        String text = resumeText == null ? "" : resumeText;
        boolean emptyBody = !StringUtils.hasText(text.trim());

        String email = firstMatch(EMAIL, text);
        if (email != null) {
            email = email.toLowerCase(Locale.ROOT);
        }
        String phone = normalizePhone(firstMatch(PHONE, text));
        String explicitName = extractLabeledName(text);
        String fileNamePerson = extractPersonFromFileName(fileName);
        String shortLineName = firstShortLineName(text);
        String name = firstNonBlank(explicitName, fileNamePerson, shortLineName);

        String fingerprint;
        if (emptyBody) {
            // 禁止共用 sha256("") —— 空正文走 per-trace legacy key
            fingerprint = null;
        } else {
            fingerprint = sha256Hex(normalizeForHash(text));
            if (fingerprint.length() > 64) {
                fingerprint = fingerprint.substring(0, 64);
            }
        }

        if (StringUtils.hasText(email)) {
            return verified(name, email, phone, "email:" + email, "EMAIL", fingerprint);
        }
        if (StringUtils.hasText(phone)) {
            return verified(name, email, phone, "phone:" + phone, "PHONE", fingerprint);
        }
        // File-name person is a stable identity even when resume body is empty —
        // do not fall through to per-trace LEGACY (that inflates uniqueCandidates).
        if (StringUtils.hasText(fileNamePerson) && fileNamePerson.length() >= 2) {
            String key = "name:file:" + fileNamePerson.toLowerCase(Locale.ROOT);
            return new IdentityHints(fileNamePerson, email, phone, key, "FILE_NAME",
                    fingerprint, 0.55, false);
        }
        if (StringUtils.hasText(name) && name.length() >= 2 && !emptyBody && fingerprint != null) {
            String key = "name:" + name.toLowerCase(Locale.ROOT) + ":"
                    + fingerprint.substring(0, Math.min(16, fingerprint.length()));
            return medium(name, email, phone, key, fingerprint);
        }

        String legacyId = StringUtils.hasText(traceId) ? traceId.trim() : "unknown";
        String display = firstNonBlank(cleanFileName(fileName), name);
        return unverified(display, email, phone, "legacy:trace:" + legacyId, fingerprint);
    }

    private static IdentityHints verified(String name, String email, String phone,
                                          String key, String source, String fingerprint) {
        return new IdentityHints(name, email, phone, key, source, fingerprint, 0.95, false);
    }

    private static IdentityHints medium(String name, String email, String phone,
                                        String key, String fingerprint) {
        return new IdentityHints(name, email, phone, key, "NAME", fingerprint, 0.65, false);
    }

    private static IdentityHints unverified(String name, String email, String phone,
                                            String key, String fingerprint) {
        return new IdentityHints(name, email, phone, key, "LEGACY_UNVERIFIED", fingerprint, 0.25, true);
    }

    private static String extractLabeledName(String text) {
        if (!StringUtils.hasText(text)) {
            return null;
        }
        Matcher label = NAME_LABEL.matcher(text);
        if (label.find()) {
            String cleaned = cleanName(label.group(1));
            if (cleaned != null && !isReservedHeading(cleaned)) {
                return cleaned;
            }
        }
        return null;
    }

    /** 黄义健的简历 (4).pdf -> 黄义健 */
    public static String extractPersonFromFileName(String fileName) {
        String base = stripPathAndExt(fileName);
        if (base == null) {
            return null;
        }
        // 去掉常见后缀噪音后再匹配
        String normalized = base.replaceAll("(?i)[_-]?(resume|cv)", " ").replaceAll("[|_]+", " ").trim();
        Matcher m = FILE_NAME_PERSON.matcher(normalized);
        if (m.matches()) {
            return cleanName(m.group(1));
        }
        return null;
    }

    private static String firstShortLineName(String text) {
        if (!StringUtils.hasText(text)) {
            return null;
        }
        String[] lines = text.split("\\R");
        for (int i = 0; i < Math.min(lines.length, 8); i++) {
            String line = lines[i].trim();
            if (line.isEmpty() || line.length() > 40) {
                continue;
            }
            if (isReservedHeading(line)) {
                continue;
            }
            if (line.contains("@") || line.matches(".*\\d{5,}.*")) {
                continue;
            }
            if (line.matches("[\\u4e00-\\u9fa5·]{2,8}")
                    || line.matches("[A-Za-z][A-Za-z\\s.'-]{1,38}")) {
                return cleanName(line);
            }
        }
        return null;
    }

    public static boolean isReservedHeading(String line) {
        if (!StringUtils.hasText(line)) {
            return false;
        }
        String n = line.replaceAll("[\\s:：\\-_|/]+", "").trim();
        return RESERVED_HEADINGS.contains(n) || RESERVED_HEADINGS.contains(line.trim());
    }

    private static String cleanFileName(String fileName) {
        String n = stripPathAndExt(fileName);
        if (n == null) {
            return null;
        }
        n = n.replaceAll("(?i)[_-]?(resume|cv|简历)", " ").replaceAll("[|_]+", " ").trim();
        // 去掉 (4) 这类副本序号
        n = n.replaceAll("\\s*[（(]\\d+[）)]\\s*$", "").trim();
        if (n.isBlank() || isReservedHeading(n)) {
            return null;
        }
        return trimTo(n, 64);
    }

    private static String stripPathAndExt(String fileName) {
        if (!StringUtils.hasText(fileName)) {
            return null;
        }
        String n = fileName;
        int slash = Math.max(n.lastIndexOf('/'), n.lastIndexOf('\\'));
        if (slash >= 0) {
            n = n.substring(slash + 1);
        }
        int dot = n.lastIndexOf('.');
        if (dot > 0) {
            n = n.substring(0, dot);
        }
        return n.isBlank() ? null : n.trim();
    }

    private static String cleanName(String raw) {
        if (!StringUtils.hasText(raw)) {
            return null;
        }
        String n = raw.replaceAll("\\s+", " ").trim();
        if (n.isBlank() || isReservedHeading(n)) {
            return null;
        }
        return trimTo(n, 64);
    }

    private static String normalizePhone(String raw) {
        if (!StringUtils.hasText(raw)) {
            return null;
        }
        String digits = raw.replaceAll("\\D", "");
        if (digits.startsWith("86") && digits.length() == 13) {
            digits = digits.substring(2);
        }
        return digits.length() >= 7 ? digits : null;
    }

    private static String normalizeForHash(String text) {
        if (!StringUtils.hasText(text)) {
            return "";
        }
        return text.toLowerCase(Locale.ROOT).replaceAll("\\s+", " ").trim();
    }

    private static String sha256Hex(String input) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] dig = md.digest(input.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(dig);
        } catch (Exception e) {
            return Integer.toHexString(input.hashCode());
        }
    }

    private static String firstMatch(Pattern pattern, String text) {
        if (!StringUtils.hasText(text)) {
            return null;
        }
        Matcher m = pattern.matcher(text);
        return m.find() ? m.group() : null;
    }

    private static String firstNonBlank(String... values) {
        if (values == null) {
            return null;
        }
        for (String v : values) {
            if (StringUtils.hasText(v)) {
                return v;
            }
        }
        return null;
    }

    private static String trimTo(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max);
    }

    /** 测试辅助：包装 Optional 风格 short-line 过滤。 */
    static Optional<String> firstShortLine(String text) {
        return Optional.ofNullable(firstShortLineName(text));
    }
}
