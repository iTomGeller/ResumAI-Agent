package com.resumai.agent.ai.agents;

import dev.langchain4j.service.SystemMessage;
import dev.langchain4j.service.UserMessage;
import dev.langchain4j.service.V;

public interface JdMatchAgent {

    @SystemMessage("""
            你是岗位匹配专家。你的任务是为候选人找到最匹配的岗位并提取结构化要求。
            
            工作流程：
            1. 使用 milvus_jd_search 工具，传入简历中的核心技能关键词，搜索匹配岗位（topK=3）
            2. 对最佳匹配岗位使用 jd_requirements_extract 提取结构化要求
            3. 综合分析匹配度并输出结果
            
            输出格式（严格 JSON，不要 markdown 包裹）：
            {"matchedJd":"最匹配岗位名称","matchScore":0.85,"requirements":["必备要求1"],"preferredSkills":["加分项1"],"gaps":["候选人缺少的能力1"]}
            """)
    String matchJd(@UserMessage @V("resumeText") String resumeText);
}
