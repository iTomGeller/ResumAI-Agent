package com.resumai.agent.ai.agents;

import dev.langchain4j.service.SystemMessage;
import dev.langchain4j.service.UserMessage;
import dev.langchain4j.service.V;

public interface ResumeParseAgent {

    @SystemMessage("""
            你是简历结构化解析专家。你的任务是将非结构化简历文本解析为标准化 JSON 结构。
            
            使用 resume_structure_extract 工具进行深度解析，提取以下维度：
            - 基本信息（姓名、联系方式、求职意向）
            - 教育背景（学校、专业、学历、时间段）
            - 工作/实习经历（公司、职位、时间段、职责）
            - 项目经验（项目名称、技术栈、职责、成果）
            - 技能清单（编程语言、框架、工具、评级）
            
            输出格式（严格 JSON）：
            {"basicInfo":{"name":"","phone":"","email":"","targetRole":""},"education":[{"school":"","major":"","degree":"","startDate":"","endDate":""}],"experiences":[{"company":"","role":"","startDate":"","endDate":"","responsibilities":[""]}],"projects":[{"name":"","techStack":[""],"role":"","achievements":[""]}],"skills":[{"category":"","items":[{"name":"","level":""}]}]}
            """)
    String parse(@UserMessage @V("resumeText") String resumeText);
}
