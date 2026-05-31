package com.resumai.agent.config;

import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.output.Response;
import java.util.List;

/**
 * 占位 EmbeddingModel：在未配置可用 provider 时装配，避免误调用外部 API。
 */
public class NoopEmbeddingModel implements EmbeddingModel {

    public static final String DISABLED_MESSAGE = "embedding disabled";

    @Override
    public Response<Embedding> embed(String text) {
        throw new IllegalStateException(DISABLED_MESSAGE);
    }

    @Override
    public Response<Embedding> embed(TextSegment textSegment) {
        throw new IllegalStateException(DISABLED_MESSAGE);
    }

    @Override
    public Response<List<Embedding>> embedAll(List<TextSegment> textSegments) {
        throw new IllegalStateException(DISABLED_MESSAGE);
    }
}
