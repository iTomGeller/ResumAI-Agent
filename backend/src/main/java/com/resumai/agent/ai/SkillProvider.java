package com.resumai.agent.ai;

import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.model.chat.request.json.JsonObjectSchema;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import static java.util.stream.Collectors.joining;

/**
 * Skill system following the Agent Skills open specification (agentskills.io).
 * Scans a directory of standard SKILL.md skill packages and exposes them to the Agent Loop.
 */
@Component
public class SkillProvider {

    private static final Logger log = LoggerFactory.getLogger(SkillProvider.class);

    @Value("${app.skills.path:skills}")
    private String skillsRootPath;

    public String getSkillsRootPath() {
        return skillsRootPath;
    }

    private volatile List<SkillDescriptor> cachedSkills = List.of();

    @PostConstruct
    @Scheduled(fixedDelay = 30000)
    public void scanSkills() {
        Path root = resolveSkillsRoot();
        if (!Files.isDirectory(root)) {
            log.debug("Skills directory not found: {}", root.toAbsolutePath());
            cachedSkills = List.of();
            return;
        }
        try (Stream<Path> dirs = Files.list(root)) {
            cachedSkills = dirs
                    .filter(Files::isDirectory)
                    .filter(dir -> Files.exists(dir.resolve("SKILL.md")))
                    .map(this::parseSkillDirectory)
                    .filter(Objects::nonNull)
                    .toList();
            log.info("Loaded {} skills from {}", cachedSkills.size(), root.toAbsolutePath());
        } catch (IOException e) {
            log.warn("Failed to scan skills directory: {}", e.getMessage());
        }
    }

    private Path resolveSkillsRoot() {
        Path configured = Path.of(skillsRootPath);
        if (Files.isDirectory(configured)) {
            return configured;
        }
        for (Path fallback : List.of(
                Path.of("src/main/resources/skills"),
                Path.of("backend/src/main/resources/skills"),
                Path.of("skills"))) {
            if (Files.isDirectory(fallback)) {
                return fallback;
            }
        }
        return configured;
    }

    private SkillDescriptor parseSkillDirectory(Path skillDir) {
        try {
            String content = Files.readString(skillDir.resolve("SKILL.md"));
            Map<String, String> frontmatter = parseFrontmatter(content);
            String body = extractBody(content);

            String name = frontmatter.getOrDefault("name", skillDir.getFileName().toString());
            String description = frontmatter.getOrDefault("description", "");
            List<String> allowedTools = frontmatter.containsKey("allowed-tools")
                    ? Arrays.asList(frontmatter.get("allowed-tools").split("\\s+"))
                    : List.of();

            Map<String, String> metadata = new LinkedHashMap<>();
            frontmatter.forEach((k, v) -> {
                if (!Set.of("name", "description", "allowed-tools", "license", "compatibility").contains(k)) {
                    metadata.put(k, v);
                }
            });

            return new SkillDescriptor(name, description, allowedTools, metadata, body, skillDir);
        } catch (IOException e) {
            log.warn("Failed to parse skill at {}: {}", skillDir, e.getMessage());
            return null;
        }
    }

    public List<SkillDescriptor> listInstalled() {
        return cachedSkills;
    }

    public boolean hasSkills() {
        return !cachedSkills.isEmpty();
    }

    /**
     * Advertise stage: expose load_skill / execute_skill / read_skill_resource as tools.
     */
    public List<ToolSpecification> getAdvertisedSpecs() {
        if (cachedSkills.isEmpty()) return List.of();

        List<ToolSpecification> specs = new ArrayList<>();

        String skillList = cachedSkills.stream()
                .map(s -> "- " + s.name() + ": " + s.description())
                .collect(joining("\n"));

        specs.add(ToolSpecification.builder()
                .name("load_skill")
                .description("加载 Skill 的完整指令文档。可用 skills:\n" + skillList)
                .parameters(JsonObjectSchema.builder()
                        .addStringProperty("skill_name", "要加载的 skill 名称")
                        .build())
                .build());

        specs.add(ToolSpecification.builder()
                .name("execute_skill")
                .description("执行一个 Skill，作为独立子 Agent 运行并返回结果")
                .parameters(JsonObjectSchema.builder()
                        .addStringProperty("skill_name", "要执行的 skill 名称")
                        .addStringProperty("task", "传递给 skill 的具体任务描述")
                        .build())
                .build());

        specs.add(ToolSpecification.builder()
                .name("read_skill_resource")
                .description("读取 Skill 目录下的资源文件（references/scripts/assets）")
                .parameters(JsonObjectSchema.builder()
                        .addStringProperty("skill_name", "skill 名称")
                        .addStringProperty("resource_path", "资源文件相对路径，如 references/risk_patterns.json")
                        .build())
                .build());

        return specs;
    }

    public boolean supports(String toolName) {
        return Set.of("load_skill", "execute_skill", "read_skill_resource").contains(toolName);
    }

    public String loadSkill(String skillName) {
        SkillDescriptor skill = findByName(skillName);
        if (skill == null) return "{\"error\": \"Skill not found: " + skillName + "\"}";
        return skill.fullInstructions();
    }

    public String readResource(String skillName, String resourcePath) {
        SkillDescriptor skill = findByName(skillName);
        if (skill == null) return "{\"error\": \"Skill not found: " + skillName + "\"}";
        Path file = skill.directory().resolve(resourcePath).normalize();
        if (!file.startsWith(skill.directory())) {
            return "{\"error\": \"Path escape attempt blocked\"}";
        }
        try {
            return Files.readString(file);
        } catch (IOException e) {
            return "{\"error\": \"Resource not found: " + resourcePath + "\"}";
        }
    }

    public SkillDescriptor findByName(String skillName) {
        return cachedSkills.stream()
                .filter(s -> s.name().equals(skillName))
                .findFirst()
                .orElse(null);
    }

    /**
     * Execute a skill and return structured JSON for downstream agents (e.g. ReportAgent).
     */
    public Map<String, Object> executeStructured(String skillName, String task) {
        SkillDescriptor skill = findByName(skillName);
        if (skill == null) {
            return Map.of("skillName", skillName, "loaded", false, "error", "Skill not found");
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("skillName", skillName);
        result.put("loaded", true);
        result.put("task", task != null ? task : "");
        result.put("instructionsDigest", compact(skill.fullInstructions(), 800));
        if ("evidence_synthesis".equals(skillName)) {
            result.put("evidenceWeights", Map.of(
                    "resume_text", 0.45,
                    "jd_match", 0.20,
                    "rag", 0.15,
                    "project_depth", 0.10,
                    "risk", 0.10
            ));
            result.put("conflicts", List.of());
            result.put("missingEvidence", List.of(
                    "quantified_metrics",
                    "project_ownership_boundary",
                    "production_incident_evidence"
            ));
            result.put("reportHints", List.of(
                    "Separate candidate facts from rubric/knowledge-base standards.",
                    "Prioritize concrete project questions over generic capability questions.",
                    "Surface evidence gaps explicitly."
            ));
        }
        return result;
    }

    private String compact(String value, int max) {
        if (value == null) return "";
        return value.length() <= max ? value : value.substring(0, max);
    }

    /**
     * Parse YAML frontmatter from SKILL.md (simple key: value parsing for flat fields).
     */
    private Map<String, String> parseFrontmatter(String content) {
        Map<String, String> map = new LinkedHashMap<>();
        if (!content.startsWith("---")) return map;
        int endIdx = content.indexOf("---", 3);
        if (endIdx < 0) return map;
        String yaml = content.substring(3, endIdx).trim();
        StringBuilder currentValue = new StringBuilder();
        String currentKey = null;
        for (String line : yaml.split("\n")) {
            if (line.matches("^[a-zA-Z][a-zA-Z0-9_-]*:.*")) {
                if (currentKey != null) {
                    map.put(currentKey, currentValue.toString().trim());
                }
                int colonIdx = line.indexOf(':');
                currentKey = line.substring(0, colonIdx).trim();
                currentValue = new StringBuilder(line.substring(colonIdx + 1).trim());
            } else if (currentKey != null) {
                currentValue.append(" ").append(line.trim());
            }
        }
        if (currentKey != null) {
            map.put(currentKey, currentValue.toString().trim());
        }
        return map;
    }

    private String extractBody(String content) {
        if (!content.startsWith("---")) return content;
        int endIdx = content.indexOf("---", 3);
        if (endIdx < 0) return content;
        return content.substring(endIdx + 3).trim();
    }
}
