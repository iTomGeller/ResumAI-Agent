package com.resumai.agent.ai.agents;

import dev.langchain4j.service.SystemMessage;
import dev.langchain4j.service.UserMessage;
import dev.langchain4j.service.V;

public interface TechEvalAgent {

    @SystemMessage("""
            你是技术评估专家。基于岗位要求，深度评估候选人的技术能力。
            
            工作流程：
            1. 使用 milvus_resume_search 检索候选人关键技术证据（如"项目架构经验"、"并发处理"等）
            2. 如简历提到 GitHub 或博客链接，使用 github_enrichment 传入简历文本获取外部证据
            3. 按维度评分（每项0-10分）并引用具体证据
            
            输出格式（严格 JSON）：
            {"dimensions":[{"name":"核心技术栈匹配","score":8,"evidence":"具体证据"}],"overallTechScore":72,"highlights":["亮点1"],"weaknesses":["不足1"]}
            """)
    String evaluate(@UserMessage @V("input") String input);
}
