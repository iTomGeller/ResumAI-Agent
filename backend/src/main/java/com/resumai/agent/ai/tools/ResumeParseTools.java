package com.resumai.agent.ai.tools;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class ResumeParseTools {

    private final ObjectMapper objectMapper;

    public ResumeParseTools(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @Tool("深度解析简历文本，提取结构化信息（教育、经历、技能、项目），返回标准化 JSON")
    public String resume_structure_extract(
            @P("简历全文文本") String resumeText) {
        try {
            Map<String, Object> parsed = new LinkedHashMap<>();

            parsed.put("textLength", resumeText.length());
            parsed.put("sections", detectSections(resumeText));
            parsed.put("skillKeywords", extractSkillKeywords(resumeText));
            parsed.put("timelineEntries", extractTimeline(resumeText));

            return objectMapper.writeValueAsString(parsed);
        } catch (Exception e) {
            return "{\"error\": \"Resume parse failed: " + e.getMessage() + "\"}";
        }
    }

    private List<String> detectSections(String text) {
        List<String> sections = new java.util.ArrayList<>();
        if (text.contains("教育背景") || text.contains("教育经历")) sections.add("education");
        if (text.contains("工作经历") || text.contains("实习经历")) sections.add("experience");
        if (text.contains("项目经验") || text.contains("项目经历")) sections.add("projects");
        if (text.contains("技能") || text.contains("技术栈")) sections.add("skills");
        if (text.contains("自我评价")) sections.add("self_evaluation");
        return sections;
    }

    private List<String> extractSkillKeywords(String text) {
        List<String> keywords = new java.util.ArrayList<>();
        String[] techs = {"Java", "Python", "Spring", "MySQL", "Redis", "Docker", "Kubernetes",
                "React", "Vue", "Go", "Rust", "C++", "TensorFlow", "PyTorch", "LLM", "RAG",
                "Milvus", "Neo4j", "Kafka", "RabbitMQ", "ElasticSearch", "MongoDB"};
        for (String tech : techs) {
            if (text.toLowerCase().contains(tech.toLowerCase())) {
                keywords.add(tech);
            }
        }
        return keywords;
    }

    private List<Map<String, String>> extractTimeline(String text) {
        List<Map<String, String>> entries = new java.util.ArrayList<>();
        Pattern pattern = Pattern.compile("(\\d{4})[-.~年](\\d{1,2})?\\s*[~—–-]+\\s*(\\d{4})?[-.~年]?(\\d{1,2})?");
        Matcher matcher = pattern.matcher(text);
        while (matcher.find()) {
            Map<String, String> entry = new LinkedHashMap<>();
            entry.put("startYear", matcher.group(1));
            entry.put("startMonth", matcher.group(2) != null ? matcher.group(2) : "");
            entry.put("endYear", matcher.group(3) != null ? matcher.group(3) : "present");
            entry.put("endMonth", matcher.group(4) != null ? matcher.group(4) : "");
            entries.add(entry);
        }
        return entries;
    }
}
