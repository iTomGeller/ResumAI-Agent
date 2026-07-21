package com.resumai.agent.config;

import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

@Configuration
public class MilvusConfig {

    private static final Logger log = LoggerFactory.getLogger(MilvusConfig.class);

    @Bean
    @Primary
    public MilvusEmbeddingStore milvusEmbeddingStore(MilvusProperties props, EmbeddingProperties embeddingProps) {
        int dimension = props.getDimension() > 0 ? props.getDimension() : embeddingProps.resolveDimension();
        String collection = props.getCollection();
        if (collection == null || collection.isBlank() || "resume_chunk".equals(collection)) {
            collection = "resume_chunk_" + embeddingProps.resolveJdCollectionSuffix();
        }
        try {
            return MilvusEmbeddingStore.builder()
                    .host(props.getHost())
                    .port(props.getPort())
                    .collectionName(collection)
                    .dimension(dimension)
                    .build();
        } catch (Exception e) {
            log.warn("[milvus] Failed to connect to Milvus ({}:{}), vector search disabled: {}",
                    props.getHost(), props.getPort(), e.getMessage());
            return null;
        }
    }

    @Bean
    @Qualifier("jdEmbeddingStore")
    public MilvusEmbeddingStore jdEmbeddingStore(MilvusProperties props, EmbeddingProperties embeddingProps) {
        int dimension = props.getDimension() > 0 ? props.getDimension() : embeddingProps.resolveDimension();
        String jdCollection = props.getJdCollection();
        if (jdCollection == null || jdCollection.isBlank() || "jd_library".equals(jdCollection)) {
            jdCollection = "jd_library_" + embeddingProps.resolveJdCollectionSuffix();
        }
        try {
            return MilvusEmbeddingStore.builder()
                    .host(props.getHost())
                    .port(props.getPort())
                    .collectionName(jdCollection)
                    .dimension(dimension)
                    .build();
        } catch (Exception e) {
            log.warn("[milvus] Failed to connect to Milvus for JD store, JD RAG disabled: {}", e.getMessage());
            return null;
        }
    }

    /** Long-term memory semantic index (recall source only; MySQL stays authoritative). */
    @Bean
    @Qualifier("memoryEmbeddingStore")
    public MilvusEmbeddingStore memoryEmbeddingStore(MilvusProperties props, EmbeddingProperties embeddingProps) {
        int dimension = props.getDimension() > 0 ? props.getDimension() : embeddingProps.resolveDimension();
        String collection = "agent_memory_" + embeddingProps.resolveJdCollectionSuffix();
        try {
            return MilvusEmbeddingStore.builder()
                    .host(props.getHost())
                    .port(props.getPort())
                    .collectionName(collection)
                    .dimension(dimension)
                    .build();
        } catch (Exception e) {
            log.warn("[milvus] memory vector store unavailable, lexical-only recall: {}", e.getMessage());
            return null;
        }
    }

    /** Knowledge-base chunk vector index (K4: unified retrieval pipeline). */
    @Bean
    @Qualifier("kbEmbeddingStore")
    public MilvusEmbeddingStore kbEmbeddingStore(MilvusProperties props, EmbeddingProperties embeddingProps) {
        // Always follow embedding provider dim — MILVUS_DIMENSION leftovers (e.g. 384 from
        // MiniLM) must not create a collection named *_1024 with the wrong vector size.
        int dimension = embeddingProps.resolveDimension();
        String collection = "kb_chunks_" + embeddingProps.resolveJdCollectionSuffix();
        try {
            return MilvusEmbeddingStore.builder()
                    .host(props.getHost())
                    .port(props.getPort())
                    .collectionName(collection)
                    .dimension(dimension)
                    .build();
        } catch (Exception e) {
            log.warn("[milvus] knowledge-base vector store unavailable, lexical-only: {}", e.getMessage());
            return null;
        }
    }
}
