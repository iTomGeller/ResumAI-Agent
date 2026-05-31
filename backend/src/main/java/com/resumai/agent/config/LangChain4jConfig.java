package com.resumai.agent.config;

import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.embedding.onnx.allminilml6v2.AllMiniLmL6V2EmbeddingModel;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.model.openai.OpenAiEmbeddingModel;
import java.time.Duration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.util.StringUtils;

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
    @Primary
    @ConditionalOnProperty(prefix = "resumai.embedding", name = "provider", havingValue = "local", matchIfMissing = true)
    public EmbeddingModel localEmbeddingModel() {
        return new AllMiniLmL6V2EmbeddingModel();
    }

    @Bean
    @Primary
    @ConditionalOnProperty(prefix = "resumai.embedding", name = "provider", havingValue = "openai")
    public EmbeddingModel openAiEmbeddingModel(EmbeddingProperties embeddingProps) {
        if (!StringUtils.hasText(embeddingProps.getApiKey())) {
            return new NoopEmbeddingModel();
        }
        return OpenAiEmbeddingModel.builder()
                .baseUrl(embeddingProps.getBaseUrl())
                .apiKey(embeddingProps.getApiKey())
                .modelName(embeddingProps.getModel())
                .timeout(Duration.ofMillis(embeddingProps.getReadTimeoutMs()))
                .maxRetries(1)
                .build();
    }

    @Bean
    @Primary
    @ConditionalOnProperty(prefix = "resumai.embedding", name = "provider", havingValue = "none")
    public EmbeddingModel noopEmbeddingModel() {
        return new NoopEmbeddingModel();
    }

    @Bean
    @Primary
    @ConditionalOnProperty(prefix = "resumai.embedding", name = "provider", havingValue = "bailian")
    public EmbeddingModel bailianEmbeddingModel(EmbeddingProperties embeddingProps) {
        if (!StringUtils.hasText(embeddingProps.getApiKey())) {
            return new NoopEmbeddingModel();
        }
        return OpenAiEmbeddingModel.builder()
                .baseUrl(StringUtils.hasText(embeddingProps.getBaseUrl()) ? embeddingProps.getBaseUrl() : "https://dashscope.aliyuncs.com/compatible-mode/v1")
                .apiKey(embeddingProps.getApiKey())
                .modelName(StringUtils.hasText(embeddingProps.getModel()) ? embeddingProps.getModel() : "text-embedding-v3")
                .timeout(Duration.ofMillis(embeddingProps.getReadTimeoutMs()))
                .maxRetries(1)
                .build();
    }

    @Bean
    @Primary
    @ConditionalOnProperty(prefix = "resumai.embedding", name = "provider", havingValue = "zhipu")
    public EmbeddingModel zhipuEmbeddingModel(EmbeddingProperties embeddingProps) {
        if (!StringUtils.hasText(embeddingProps.getApiKey())) {
            return new NoopEmbeddingModel();
        }
        return OpenAiEmbeddingModel.builder()
                .baseUrl(StringUtils.hasText(embeddingProps.getBaseUrl()) ? embeddingProps.getBaseUrl() : "https://open.bigmodel.cn/api/paas/v4")
                .apiKey(embeddingProps.getApiKey())
                .modelName(StringUtils.hasText(embeddingProps.getModel()) ? embeddingProps.getModel() : "embedding-3")
                .timeout(Duration.ofMillis(embeddingProps.getReadTimeoutMs()))
                .maxRetries(1)
                .build();
    }
}
