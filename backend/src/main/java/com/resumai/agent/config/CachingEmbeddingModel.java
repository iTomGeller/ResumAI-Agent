package com.resumai.agent.config;

import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.output.Response;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.redisson.api.RBucket;
import org.redisson.api.RedissonClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Content-hash Redis cache in front of a remote embedding provider: the same
 * text + model never gets billed twice (30d TTL). Cache failures fall through
 * to the delegate — never the other way around.
 */
public class CachingEmbeddingModel implements EmbeddingModel {

    private static final Logger log = LoggerFactory.getLogger(CachingEmbeddingModel.class);
    private static final long TTL_DAYS = 30;

    private final EmbeddingModel delegate;
    private final RedissonClient redisson;
    private final String modelName;

    public CachingEmbeddingModel(EmbeddingModel delegate, RedissonClient redisson, String modelName) {
        this.delegate = delegate;
        this.redisson = redisson;
        this.modelName = modelName;
    }

    @Override
    public Response<Embedding> embed(String text) {
        Embedding cached = readCache(text);
        if (cached != null) {
            return Response.from(cached);
        }
        Response<Embedding> response = delegate.embed(text);
        writeCache(text, response.content());
        return response;
    }

    @Override
    public Response<Embedding> embed(TextSegment segment) {
        return embed(segment.text());
    }

    @Override
    public Response<List<Embedding>> embedAll(List<TextSegment> segments) {
        List<Embedding> out = new ArrayList<>(segments.size());
        List<TextSegment> misses = new ArrayList<>();
        List<Integer> missIndex = new ArrayList<>();
        for (int i = 0; i < segments.size(); i++) {
            Embedding cached = readCache(segments.get(i).text());
            out.add(cached);
            if (cached == null) {
                misses.add(segments.get(i));
                missIndex.add(i);
            }
        }
        if (!misses.isEmpty()) {
            Response<List<Embedding>> fresh = delegate.embedAll(misses);
            List<Embedding> embeddings = fresh.content();
            for (int j = 0; j < embeddings.size() && j < missIndex.size(); j++) {
                out.set(missIndex.get(j), embeddings.get(j));
                writeCache(misses.get(j).text(), embeddings.get(j));
            }
        }
        return Response.from(out);
    }

    private String key(String text) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            String hash = HexFormat.of().formatHex(
                    digest.digest(text.getBytes(StandardCharsets.UTF_8)));
            return "resumai:embed:" + modelName + ":" + hash.substring(0, 40);
        } catch (Exception e) {
            return null;
        }
    }

    private Embedding readCache(String text) {
        String key = key(text);
        if (key == null) {
            return null;
        }
        try {
            RBucket<float[]> bucket = redisson.getBucket(key);
            float[] vector = bucket.get();
            return vector != null ? Embedding.from(vector) : null;
        } catch (Exception e) {
            log.debug("embedding cache read skipped: {}", e.getMessage());
            return null;
        }
    }

    private void writeCache(String text, Embedding embedding) {
        String key = key(text);
        if (key == null || embedding == null) {
            return;
        }
        try {
            RBucket<float[]> bucket = redisson.getBucket(key);
            bucket.set(embedding.vector(), TTL_DAYS, TimeUnit.DAYS);
        } catch (Exception e) {
            log.debug("embedding cache write skipped: {}", e.getMessage());
        }
    }
}
