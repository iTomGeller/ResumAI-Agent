package com.resumai.agent.service;

import com.resumai.agent.config.EmbeddingProperties;
import com.resumai.agent.config.MilvusProperties;
import io.milvus.client.MilvusServiceClient;
import io.milvus.grpc.MutationResult;
import io.milvus.param.ConnectParam;
import io.milvus.param.R;
import io.milvus.param.dml.DeleteParam;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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

    public MilvusVectorMaintenanceService(MilvusProperties milvusProperties,
                                          EmbeddingProperties embeddingProperties) {
        this.milvusProperties = milvusProperties;
        this.embeddingProperties = embeddingProperties;
    }

    private volatile MilvusServiceClient client;

    public void deleteJdVectors(String jdId) {
        if (!StringUtils.hasText(jdId)) {
            return;
        }
        deleteByExpr(jdCollection(), "jdId == \"" + escape(jdId) + "\"");
    }

    public void deleteResumeVectors(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return;
        }
        deleteByExpr(resumeCollection(), "traceId == \"" + escape(traceId) + "\"");
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
