package com.resumai.agent.config;

import com.resumai.agent.ai.TracingChatModelListener;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.chat.listener.ChatModelListener;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.embedding.onnx.allminilml6v2.AllMiniLmL6V2EmbeddingModel;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.model.openai.OpenAiEmbeddingModel;
import java.time.Duration;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.util.StringUtils;

@Configuration
public class LangChain4jConfig {

    @Bean
    @ConditionalOnProperty(prefix = "resumai.workflow", name = "mode", havingValue = "java")
    public ChatModel chatModelWithTracing(DeepSeekProperties props, TracingChatModelListener tracingListener) {
        return OpenAiChatModel.builder()
                .baseUrl("https://api.deepseek.com/v1")
                .apiKey(props.getApiKey() != null ? props.getApiKey() : "sk-placeholder")
                .modelName(props.getModel())
                .timeout(Duration.ofMillis(props.getReadTimeoutMs()))
                .temperature(0.2)
                .maxTokens(8192)
                .maxRetries(3)
                .listeners(List.of(tracingListener))
                .build();
    }

    @Bean
    @Primary
    @ConditionalOnProperty(prefix = "resumai.workflow", name = "mode", havingValue = "python", matchIfMissing = true)
    public ChatModel chatModelWithoutTracing(DeepSeekProperties props) {
        return OpenAiChatModel.builder()
                .baseUrl("https://api.deepseek.com/v1")
                .apiKey(props.getApiKey() != null ? props.getApiKey() : "sk-placeholder")
                .modelName(props.getModel())
                .timeout(Duration.ofMillis(props.getReadTimeoutMs()))
                .temperature(0.2)
                .maxTokens(8192)
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
    @ConditionalOnProperty(prefix = "resumai.embedding", name = "provider", havingValue = "openrouter")
    public EmbeddingModel openRouterEmbeddingModel(EmbeddingProperties embeddingProps) {
        if (!StringUtils.hasText(embeddingProps.getApiKey())) {
            return new NoopEmbeddingModel();
        }
        return OpenAiEmbeddingModel.builder()
                .baseUrl(StringUtils.hasText(embeddingProps.getBaseUrl()) ? embeddingProps.getBaseUrl() : "https://openrouter.ai/api/v1")
                .apiKey(embeddingProps.getApiKey())
                .modelName(StringUtils.hasText(embeddingProps.getModel()) ? embeddingProps.getModel() : "openai/text-embedding-3-small")
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
