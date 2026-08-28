package com.resumai.agent.conversation;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.domain.entity.ConversationSession;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Component;

/** Codec and deterministic scope guard for durable Copilot conversation memory. */
@Component
public class CopilotMemoryCodec {

    public static final String SCHEMA_VERSION = "copilot-memory-v1";

    private static final List<String> ITEM_KEYS = List.of(
            "goals", "confirmedCorrections", "decisions", "openQuestions");

    private final ObjectMapper objectMapper;

    public CopilotMemoryCodec(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public record Scope(
            int revision,
            String resumeHash,
            String jdHash,
            String scopeHash
    ) {
    }

    public Scope scopeFor(ConversationSession session) {
        int revision = session != null && session.getActiveRevision() != null
                ? Math.max(1, session.getActiveRevision()) : 1;
        String resumeHash = hash(session != null ? session.getResumeText() : "");
        String jdHash = hash(session != null ? session.getJobDescription() : "");
        return new Scope(revision, resumeHash, jdHash,
                hash(revision + ":" + resumeHash + ":" + jdHash));
    }

    /** Legacy plain-text summaries and mismatched revisions fail closed. */
    public Map<String, Object> decodeMatching(String stored, Scope expected) {
        if (stored == null || !stored.stripLeading().startsWith("{")) {
            return Map.of();
        }
        try {
            Map<String, Object> parsed = objectMapper.readValue(
                    stored, new TypeReference<>() { });
            if (!SCHEMA_VERSION.equals(String.valueOf(parsed.get("schemaVersion")))) {
                return Map.of();
            }
            Object rawScope = parsed.get("scope");
            if (!(rawScope instanceof Map<?, ?> scope)) {
                return Map.of();
            }
            if (!expected.scopeHash().equals(String.valueOf(scope.get("scopeHash")))) {
                return Map.of();
            }
            return normalizedMemory(parsed, expected,
                    longValue(parsed.get("compactedThroughMessageId")));
        } catch (Exception ignored) {
            return Map.of();
        }
    }

    public Map<String, Object> mergeForStorage(
            Map<String, Object> previous,
            Map<String, Object> providerMemory,
            Scope scope,
            Long compactedThroughMessageId) {
        Map<String, Object> merged = new LinkedHashMap<>();
        merged.put("schemaVersion", SCHEMA_VERSION);
        merged.put("scope", scopeMap(scope));
        for (String key : ITEM_KEYS) {
            merged.put(key, mergeItems(
                    previous != null ? previous.get(key) : null,
                    providerMemory != null ? providerMemory.get(key) : null,
                    "goals".equals(key) ? 6 : 8));
        }
        merged.put("evidenceRefs", mergeEvidenceRefs(
                previous != null ? previous.get("evidenceRefs") : null,
                providerMemory != null ? providerMemory.get("evidenceRefs") : null));
        if (compactedThroughMessageId != null) {
            merged.put("compactedThroughMessageId", compactedThroughMessageId);
        }
        return merged;
    }

    public String encode(Map<String, Object> memory) {
        try {
            return objectMapper.writeValueAsString(memory != null ? memory : Map.of());
        } catch (Exception e) {
            throw new IllegalArgumentException("encode Copilot memory failed", e);
        }
    }

    private Map<String, Object> normalizedMemory(
            Map<String, Object> raw, Scope scope, Long compactedThrough) {
        return mergeForStorage(Map.of(), raw, scope, compactedThrough);
    }

    private static Map<String, Object> scopeMap(Scope scope) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("revision", scope.revision());
        value.put("resumeHash", scope.resumeHash());
        value.put("jdHash", scope.jdHash());
        value.put("scopeHash", scope.scopeHash());
        return value;
    }

    private static List<Map<String, Object>> mergeItems(
            Object previous, Object incoming, int limit) {
        List<Map<String, Object>> combined = new ArrayList<>();
        appendItems(combined, previous);
        appendItems(combined, incoming);
        LinkedHashMap<String, Map<String, Object>> byText = new LinkedHashMap<>();
        for (Map<String, Object> item : combined) {
            String text = clip(String.valueOf(item.getOrDefault("text", "")), 300);
            if (text.isBlank()) {
                continue;
            }
            Map<String, Object> normalized = new LinkedHashMap<>();
            normalized.put("text", text);
            Long sourceMessageId = longValue(item.get("sourceMessageId"));
            if (sourceMessageId != null) {
                normalized.put("sourceMessageId", sourceMessageId);
            }
            String status = clip(String.valueOf(item.getOrDefault("status", "")), 40);
            if (!status.isBlank()) {
                normalized.put("status", status);
            }
            byText.remove(text);
            byText.put(text, normalized);
        }
        List<Map<String, Object>> values = new ArrayList<>(byText.values());
        return values.size() <= limit
                ? values : values.subList(values.size() - limit, values.size());
    }

    private static void appendItems(List<Map<String, Object>> out, Object raw) {
        if (!(raw instanceof List<?> list)) {
            return;
        }
        for (Object item : list) {
            if (item instanceof Map<?, ?> map) {
                Map<String, Object> copy = new LinkedHashMap<>();
                map.forEach((key, value) -> copy.put(String.valueOf(key), value));
                out.add(copy);
            } else if (item != null) {
                out.add(Map.of("text", String.valueOf(item)));
            }
        }
    }

    private static List<String> mergeEvidenceRefs(Object previous, Object incoming) {
        LinkedHashSet<String> refs = new LinkedHashSet<>();
        appendEvidenceRefs(refs, previous);
        appendEvidenceRefs(refs, incoming);
        List<String> values = new ArrayList<>(refs);
        return values.size() <= 12
                ? values : values.subList(values.size() - 12, values.size());
    }

    private static void appendEvidenceRefs(LinkedHashSet<String> out, Object raw) {
        if (!(raw instanceof List<?> list)) {
            return;
        }
        for (Object item : list) {
            String value = clip(String.valueOf(item), 160);
            if (!value.isBlank()) {
                out.add(value);
            }
        }
    }

    private static Long longValue(Object raw) {
        if (raw instanceof Number number) {
            return number.longValue();
        }
        try {
            return raw == null ? null : Long.valueOf(String.valueOf(raw));
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    private static String hash(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(String.valueOf(value == null ? "" : value)
                            .getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(24);
            for (int i = 0; i < 12; i++) {
                hex.append(String.format("%02x", digest[i]));
            }
            return hex.toString();
        } catch (Exception e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    private static String clip(String value, int limit) {
        String text = value == null ? "" : value.trim();
        return text.length() <= limit ? text : text.substring(0, limit);
    }
}
