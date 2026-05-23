package com.resumai.agent;

import com.resumai.agent.config.DeepSeekProperties;
import com.resumai.agent.config.MilvusProperties;
import com.resumai.agent.config.Neo4jProperties;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

/**
 * ResumAI Agent 后端启动入口。
 */
@SpringBootApplication
@EnableConfigurationProperties({DeepSeekProperties.class, Neo4jProperties.class, MilvusProperties.class})
@MapperScan("com.resumai.agent.dao")
public class ResumAiAgentApplication {

    public static void main(String[] args) {
        SpringApplication.run(ResumAiAgentApplication.class, args);
    }
}
