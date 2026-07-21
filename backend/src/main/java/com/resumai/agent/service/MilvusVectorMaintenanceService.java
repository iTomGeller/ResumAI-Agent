package com.resumai.agent.service;

import com.resumai.agent.config.EmbeddingProperties;
import com.resumai.agent.config.MilvusProperties;
import dev.langchain4j.store.embedding.filter.comparison.IsEqualTo;
import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import io.milvus.client.MilvusServiceClient;
import io.milvus.grpc.MutationResult;
import io.milvus.param.ConnectParam;
import io.milvus.param.R;
import io.milvus.param.dml.DeleteParam;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.lang.Nullable;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Milvus 向量维护 — JD/简历更新删除时的向量一致性清理。
 */
@Service
public class MilvusVectorMaintenanceService {

    private static final Logger log = LoggerFactory.getLogger(MilvusVectorMaintenanceService.class);

    private final MilvusProperties milvusProperties;
    private final EmbeddingProperties embeddingProperties;
    private final MilvusEmbeddingStore kbStore;
    private final MilvusEmbeddingStore jdStore;

    public MilvusVectorMaintenanceService(MilvusProperties milvusProperties,
                                          EmbeddingProperties embeddingProperties,
                                          @Nullable @Qualifier("kbEmbeddingStore") MilvusEmbeddingStore kbStore,
                                          @Nullable @Qualifier("jdEmbeddingStore") MilvusEmbeddingStore jdStore) {
        this.milvusProperties = milvusProperties;
        this.embeddingProperties = embeddingProperties;
        this.kbStore = kbStore;
        this.jdStore = jdStore;
    }

    private volatile MilvusServiceClient client;

    public void deleteJdVectors(String jdId) {
        if (!StringUtils.hasText(jdId)) {
            return;
        }
        // LangChain4j stores metadata as a JSON blob field — use store filter API.
        if (jdStore != null) {
            try {
                jdStore.removeAll(new IsEqualTo("jdId", jdId));
                log.info("Milvus JD vectors removed via filter jdId={}", jdId);
                return;
            } catch (Exception e) {
                log.warn("Milvus JD filter delete failed jdId={}: {}", jdId, e.getMessage());
            }
        }
        deleteByExpr(jdCollection(), "jdId == \"" + escape(jdId) + "\"");
    }

    public void deleteResumeVectors(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return;
        }
        deleteByExpr(resumeCollection(), "traceId == \"" + escape(traceId) + "\"");
    }

    /** Delete all vectors belonging to one knowledge-base document. */
    public void deleteKbVectors(String docId) {
        if (!StringUtils.hasText(docId)) {
            return;
        }
        if (kbStore != null) {
            try {
                kbStore.removeAll(new IsEqualTo("docId", docId));
                log.info("Milvus KB vectors removed via filter docId={}", docId);
                return;
            } catch (Exception e) {
                log.warn("Milvus KB filter delete failed docId={}: {}", docId, e.getMessage());
            }
        }
        // Fallback: JSON metadata path (may fail on older collections).
        deleteByExpr(kbCollection(), "metadata[\"docId\"] == \"" + escape(docId) + "\"");
    }

    /** Wipe the entire knowledge-base vector collection (used by full reindex). */
    public void clearKbCollection() {
        if (kbStore != null) {
            try {
                kbStore.removeAll();
                log.info("Milvus KB collection cleared");
                return;
            } catch (Exception e) {
                log.warn("Milvus KB clear failed: {}", e.getMessage());
            }
        }
        deleteByExpr(kbCollection(), "id != \"\"");
    }

    private String kbCollection() {
        return "kb_chunks_" + embeddingProperties.resolveJdCollectionSuffix();
    }

    private void deleteByExpr(String collection, String expr) {
        try {
            R<MutationResult> result = client().delete(DeleteParam.newBuilder()
                    .withCollectionName(collection)
                    .withExpr(expr)
                    .build());
            if (result.getStatus() != R.Status.Success.getCode()) {
                log.warn("Milvus delete failed (collection={}, expr={}): {}", collection, expr, result.getMessage());
            } else {
                log.info("Milvus deleted vectors (collection={}, expr={})", collection, expr);
            }
        } catch (Exception e) {
            log.warn("Milvus delete error (collection={}, expr={}): {}", collection, expr, e.getMessage());
        }
    }

    private String resumeCollection() {
        String collection = milvusProperties.getCollection();
        if (collection == null || collection.isBlank() || "resume_chunk".equals(collection)) {
            return "resume_chunk_" + embeddingProperties.resolveJdCollectionSuffix();
        }
        return collection;
    }

    private String jdCollection() {
        String jdCollection = milvusProperties.getJdCollection();
        if (jdCollection == null || jdCollection.isBlank() || "jd_library".equals(jdCollection)) {
            return "jd_library_" + embeddingProperties.resolveJdCollectionSuffix();
        }
        return jdCollection;
    }

    private MilvusServiceClient client() {
        if (client == null) {
            synchronized (this) {
                if (client == null) {
                    client = new MilvusServiceClient(ConnectParam.newBuilder()
                            .withHost(milvusProperties.getHost())
                            .withPort(milvusProperties.getPort())
                            .build());
                }
            }
        }
        return client;
    }

    private static String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
