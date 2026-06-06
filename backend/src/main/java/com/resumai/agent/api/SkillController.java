package com.resumai.agent.api;

import com.resumai.agent.ai.SkillDescriptor;
import com.resumai.agent.ai.SkillProvider;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.io.IOException;
import java.nio.file.*;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * REST API for Skill management (Agent Skills open specification).
 * Skills are standard SKILL.md directories imported from git, zip, or filesystem.
 */
@RestController
@RequestMapping("/api/skills")
public class SkillController {

    private final SkillProvider skillProvider;

    public SkillController(SkillProvider skillProvider) {
        this.skillProvider = skillProvider;
    }

    @GetMapping
    public ResponseEntity<?> listSkills() {
        List<Map<String, Object>> result = skillProvider.listInstalled().stream().map(s -> {
            Map<String, Object> info = new LinkedHashMap<>();
            info.put("name", s.name());
            info.put("description", s.description());
            info.put("allowedTools", s.allowedTools());
            info.put("metadata", s.metadata());
            info.put("directory", s.directory().toString());
            return info;
        }).toList();
        return ResponseEntity.ok(result);
    }

    @PostMapping("/import/git")
    public ResponseEntity<?> importFromGit(@RequestBody GitImportRequest request) {
        try {
            Path skillsRoot = Path.of(skillProvider.getSkillsRootPath());
            Files.createDirectories(skillsRoot);

            Path targetDir = skillsRoot.resolve(request.skillName());
            if (Files.exists(targetDir)) {
                return ResponseEntity.badRequest().body(Map.of("error", "Skill already exists: " + request.skillName()));
            }

            ProcessBuilder pb = new ProcessBuilder(
                    "git", "clone", "--depth", "1", request.gitUrl(), targetDir.toString()
            );
            pb.inheritIO();
            Process process = pb.start();
            int exitCode = process.waitFor();
            if (exitCode != 0) {
                return ResponseEntity.badRequest().body(Map.of("error", "git clone failed with exit code " + exitCode));
            }

            if (request.skillPath() != null && !request.skillPath().isEmpty()) {
                Path subPath = targetDir.resolve(request.skillPath());
                if (Files.exists(subPath.resolve("SKILL.md"))) {
                    Path finalDir = skillsRoot.resolve(request.skillName());
                    if (!subPath.equals(finalDir)) {
                        Files.move(subPath, finalDir, StandardCopyOption.REPLACE_EXISTING);
                    }
                }
            }

            if (!Files.exists(targetDir.resolve("SKILL.md"))) {
                deleteDirectory(targetDir);
                return ResponseEntity.badRequest().body(Map.of("error", "No SKILL.md found in imported directory"));
            }

            skillProvider.scanSkills();
            return ResponseEntity.ok(Map.of("imported", request.skillName(), "status", "success"));
        } catch (Exception e) {
            return ResponseEntity.internalServerError().body(Map.of("error", e.getMessage()));
        }
    }

    @DeleteMapping("/{name}")
    public ResponseEntity<?> removeSkill(@PathVariable String name) {
        try {
            Path skillDir = Path.of(skillProvider.getSkillsRootPath()).resolve(name);
            if (!Files.exists(skillDir)) {
                return ResponseEntity.notFound().build();
            }
            deleteDirectory(skillDir);
            skillProvider.scanSkills();
            return ResponseEntity.ok(Map.of("removed", name));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().body(Map.of("error", e.getMessage()));
        }
    }

    @PostMapping("/refresh")
    public ResponseEntity<?> refresh() {
        skillProvider.scanSkills();
        return ResponseEntity.ok(Map.of(
                "status", "refreshed",
                "count", skillProvider.listInstalled().size()
        ));
    }

    private void deleteDirectory(Path dir) throws IOException {
        if (Files.exists(dir)) {
            Files.walk(dir)
                    .sorted((a, b) -> b.compareTo(a))
                    .forEach(p -> {
                        try { Files.delete(p); } catch (IOException ignored) {}
                    });
        }
    }

    public record GitImportRequest(String gitUrl, String skillName, String skillPath) {}
}
