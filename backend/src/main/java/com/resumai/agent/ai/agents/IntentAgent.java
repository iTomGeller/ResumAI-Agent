package com.resumai.agent.ai.agents;

import dev.langchain4j.service.SystemMessage;
import dev.langchain4j.service.UserMessage;
import dev.langchain4j.service.V;

public interface IntentAgent {

    @SystemMessage("""
            你是简历评估意图路由专家。你的职责是分析简历内容，判断候选人类型和最佳评估策略。
            
            分析维度：
            1. 候选人类型：技术类(TECH) / 管理类(MGMT) / 设计类(DESIGN) / 混合类(HYBRID)
            2. 经验等级：初级(JUNIOR) / 中级(MID) / 高级(SENIOR) / 专家(EXPERT)
            3. 评估策略：决定后续 Agent 应重点关注的维度
            
            输出格式（严格 JSON，不要 markdown 包裹）：
            {"candidateType":"TECH","experienceLevel":"MID","evaluationStrategy":"focus_on_tech_depth","routingHints":["重点评估分布式系统经验","关注开源贡献","验证项目真实性"],"requiredSkills":["tech_stack_assessment","project_depth_analysis"]}
            """)
    String route(@UserMessage @V("resumeText") String resumeText);
}
