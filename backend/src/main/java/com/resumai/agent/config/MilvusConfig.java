package com.resumai.agent.config;

import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class MilvusConfig {

    @Bean
    public MilvusEmbeddingStore milvusEmbeddingStore(MilvusProperties props) {
        return MilvusEmbeddingStore.builder()
                .host(props.getHost())
                .port(props.getPort())
                .collectionName(props.getCollection())
                .dimension(props.getDimension())
                .build();
    }
}
