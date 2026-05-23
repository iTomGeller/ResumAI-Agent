package com.resumai.agent.config;

import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.model.openai.OpenAiEmbeddingModel;
import java.time.Duration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class LangChain4jConfig {

    @Bean
    public ChatModel chatModel(DeepSeekProperties props) {
        return OpenAiChatModel.builder()
                .baseUrl("https://api.deepseek.com/v1")
                .apiKey(props.getApiKey() != null ? props.getApiKey() : "sk-placeholder")
                .modelName(props.getModel())
                .timeout(Duration.ofMillis(props.getReadTimeoutMs()))
                .temperature(0.2)
                .maxTokens(1200)
                .maxRetries(3)
                .build();
    }

    @Bean
    public EmbeddingModel embeddingModel(DeepSeekProperties props) {
        return OpenAiEmbeddingModel.builder()
                .baseUrl("https://api.deepseek.com/v1")
                .apiKey(props.getApiKey() != null ? props.getApiKey() : "sk-placeholder")
                .modelName("text-embedding-v1")
                .timeout(Duration.ofMillis(props.getReadTimeoutMs()))
                .maxRetries(3)
                .build();
    }
}
