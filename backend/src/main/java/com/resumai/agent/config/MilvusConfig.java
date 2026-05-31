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
    public MilvusEmbeddingStore milvusEmbeddingStore(MilvusProperties props, EmbeddingProperties embeddingProps) {
        int dimension = props.getDimension() > 0 ? props.getDimension() : embeddingProps.resolveDimension();
        String collection = props.getCollection();
        if (collection == null || collection.isBlank() || "resume_chunk".equals(collection)) {
            collection = "resume_chunk_" + embeddingProps.resolveJdCollectionSuffix();
        }
        return MilvusEmbeddingStore.builder()
                .host(props.getHost())
                .port(props.getPort())
                .collectionName(collection)
                .dimension(dimension)
                .build();
    }

    @Bean
    @Qualifier("jdEmbeddingStore")
    public MilvusEmbeddingStore jdEmbeddingStore(MilvusProperties props, EmbeddingProperties embeddingProps) {
        int dimension = props.getDimension() > 0 ? props.getDimension() : embeddingProps.resolveDimension();
        String jdCollection = props.getJdCollection();
        if (jdCollection == null || jdCollection.isBlank() || "jd_library".equals(jdCollection)) {
            jdCollection = "jd_library_" + embeddingProps.resolveJdCollectionSuffix();
        }
        return MilvusEmbeddingStore.builder()
                .host(props.getHost())
                .port(props.getPort())
                .collectionName(jdCollection)
                .dimension(dimension)
                .build();
    }
}
