package com.resumai.agent.ai.tools;

import com.resumai.agent.ai.SkillDescriptor;
import com.resumai.agent.ai.SkillProvider;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * SkillExecutor — wraps the SkillProvider as @Tool methods that agents can call.
 * Skills are dynamically loaded prompt templates following agentskills.io standard.
 * When an agent calls execute_skill, it loads the Skill's instructions and returns them
 * as context for the LLM to follow.
 */
public class SkillTools {

    private static final Logger log = LoggerFactory.getLogger(SkillTools.class);
    private final SkillProvider skillProvider;

    public SkillTools(SkillProvider skillProvider) {
        this.skillProvider = skillProvider;
    }

    @Tool("加载并执行一个 Skill（可插拔 Prompt 模板），返回 Skill 的完整指令供 LLM 遵循执行")
    public String execute_skill(
            @P("要执行的 Skill 名称，如 intent_routing / tech_stack_assessment / project_depth_analysis / risk_pattern_detection / evidence_synthesis") String skillName,
            @P("传递给 Skill 的具体任务描述或输入数据") String task) {
        SkillDescriptor skill = skillProvider.findByName(skillName);
        if (skill == null) {
            log.warn("Skill not found: {}", skillName);
            return "{\"error\": \"Skill not found: " + skillName + "\", \"available\": " + listAvailable() + "}";
        }
        log.info("Executing skill: {} for task length: {}", skillName, task.length());
        return "【Skill: " + skillName + " v" + skill.metadata().getOrDefault("version", "1.0") + "】\n\n"
                + skill.fullInstructions() + "\n\n--- 任务输入 ---\n" + truncate(task, 2000);
    }

    @Tool("查看所有已安装的 Skills 列表，了解当前系统可用的 Prompt 模板能力")
    public String list_skills() {
        return listAvailable();
    }

    private String listAvailable() {
        var skills = skillProvider.listInstalled();
        if (skills.isEmpty()) return "[\"暂无已安装 Skills\"]";
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < skills.size(); i++) {
            var s = skills.get(i);
            if (i > 0) sb.append(",");
            sb.append("{\"name\":\"").append(s.name())
                    .append("\",\"description\":\"").append(s.description()).append("\"}");
        }
        sb.append("]");
        return sb.toString();
    }

    private String truncate(String s, int maxLen) {
        return s != null && s.length() > maxLen ? s.substring(0, maxLen) + "..." : s;
    }
}
