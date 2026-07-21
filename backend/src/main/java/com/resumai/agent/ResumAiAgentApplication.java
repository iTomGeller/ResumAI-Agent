package com.resumai.agent;

import com.resumai.agent.config.DeepSeekProperties;
import com.resumai.agent.config.EmbeddingProperties;
import com.resumai.agent.config.MilvusProperties;
import com.resumai.agent.config.MysqlObservabilityProperties;
import com.resumai.agent.config.ObjectStorageProperties;
import com.resumai.agent.config.TaskQueueProperties;
import com.resumai.agent.config.WorkflowProperties;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * ResumAI Agent 后端启动入口。
 */
@SpringBootApplication
@EnableScheduling
@EnableConfigurationProperties({
        DeepSeekProperties.class,
        MilvusProperties.class,
        EmbeddingProperties.class,
        MysqlObservabilityProperties.class,
        ObjectStorageProperties.class,
        TaskQueueProperties.class,
        WorkflowProperties.class
})
@MapperScan("com.resumai.agent.dao")
public class ResumAiAgentApplication {

    public static void main(String[] args) {
        SpringApplication.run(ResumAiAgentApplication.class, args);
    }
}
