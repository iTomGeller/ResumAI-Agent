package com.resumai.agent.ai;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * Parsed representation of a standard SKILL.md directory (agentskills.io open spec).
 */
public record SkillDescriptor(
        String name,
        String description,
        List<String> allowedTools,
        Map<String, String> metadata,
        String fullInstructions,
        Path directory
) {
}
