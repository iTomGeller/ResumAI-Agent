package com.resumai.agent.config;

import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

@Configuration
public class MilvusConfig {

    @Bean
    @Primary
    public MilvusEmbeddingStore milvusEmbeddingStore(MilvusProperties props) {
        return MilvusEmbeddingStore.builder()
                .host(props.getHost())
                .port(props.getPort())
                .collectionName(props.getCollection())
                .dimension(props.getDimension())
                .build();
    }

    @Bean
    @Qualifier("jdEmbeddingStore")
    public MilvusEmbeddingStore jdEmbeddingStore(MilvusProperties props) {
        return MilvusEmbeddingStore.builder()
                .host(props.getHost())
                .port(props.getPort())
                .collectionName(props.getJdCollection())
                .dimension(props.getDimension())
                .build();
    }
}
