package com.resumai.agent.service;

import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.api.dto.JdMatchResult;
import dev.langchain4j.data.document.Document;
import dev.langchain4j.data.document.Metadata;
import dev.langchain4j.data.document.splitter.DocumentSplitters;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingMatch;
import dev.langchain4j.store.embedding.EmbeddingSearchRequest;
import dev.langchain4j.store.embedding.EmbeddingSearchResult;
import dev.langchain4j.store.embedding.milvus.MilvusEmbeddingStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * RAG service for JD (Job Description) indexing and matching.
 * Provides automatic JD matching when HR uploads a resume without selecting a position,
 * and structured requirement extraction for gap analysis.
 */
@Service
public class JdRagService {

    private static final Logger log = LoggerFactory.getLogger(JdRagService.class);

    private final MilvusEmbeddingStore jdEmbeddingStore;
    private final EmbeddingModel embeddingModel;
    private final DeepSeekClient deepSeekClient;

    private final Map<String, JdMeta> jdMetaCache = new ConcurrentHashMap<>();

    public JdRagService(@Qualifier("jdEmbeddingStore") MilvusEmbeddingStore jdEmbeddingStore,
                        EmbeddingModel embeddingModel,
                        DeepSeekClient deepSeekClient) {
        this.jdEmbeddingStore = jdEmbeddingStore;
        this.embeddingModel = embeddingModel;
        this.deepSeekClient = deepSeekClient;
    }

    /**
     * Index a JD into the vector store for later similarity matching.
     */
    public void indexJd(String jdId, String title, String category, String jdText) {
        if (!StringUtils.hasText(jdText)) return;
        try {
            jdMetaCache.put(jdId, new JdMeta(jdId, title, category));

            String fullText = "岗位: " + title + "\n类别: " + category + "\n" + jdText;
            Document doc = Document.from(fullText, Metadata.from(Map.of("jdId", jdId, "title", title, "category", category)));
            var splitter = DocumentSplitters.recursive(400, 80);
            List<TextSegment> segments = splitter.split(doc);

            List<Embedding> embeddings = embeddingModel.embedAll(segments).content();
            jdEmbeddingStore.addAll(embeddings, segments);
            log.info("Indexed JD '{}' ({} chunks) into jd_library", title, segments.size());
        } catch (Exception e) {
            log.warn("Failed to index JD '{}': {}", title, e.getMessage());
        }
    }

    /**
     * Given resume text, find the top-N most relevant JDs from the vector store.
     */
    public List<JdMatchResult> matchTopJds(String resumeText, int topK) {
        if (!StringUtils.hasText(resumeText)) return List.of();
        try {
            String queryText = resumeText.length() > 2000 ? resumeText.substring(0, 2000) : resumeText;
            Embedding queryEmbedding = embeddingModel.embed(queryText).content();
            EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                    .queryEmbedding(queryEmbedding)
                    .maxResults(topK * 3)
                    .minScore(0.4)
                    .build();
            EmbeddingSearchResult<TextSegment> result = jdEmbeddingStore.search(request);
            List<EmbeddingMatch<TextSegment>> matches = result.matches();

            Map<String, JdMatchResult> deduped = new java.util.LinkedHashMap<>();
            for (EmbeddingMatch<TextSegment> match : matches) {
                TextSegment seg = match.embedded();
                if (seg == null || seg.metadata() == null) continue;
                String jdId = seg.metadata().getString("jdId");
                String title = seg.metadata().getString("title");
                String category = seg.metadata().getString("category");
                if (jdId == null) continue;
                if (!deduped.containsKey(jdId)) {
                    deduped.put(jdId, new JdMatchResult(jdId, title != null ? title : "", category != null ? category : "", match.score()));
                }
                if (deduped.size() >= topK) break;
            }
            return new ArrayList<>(deduped.values());
        } catch (Exception e) {
            log.warn("JD matching failed: {}", e.getMessage());
            return List.of();
        }
    }

    /**
     * Extract structured requirements from a JD text using LLM.
     * Returns a formatted string of requirements for gap analysis.
     */
    public String extractRequirements(String jdText) {
        if (!StringUtils.hasText(jdText)) return "";
        try {
            String prompt = """
                    请从以下岗位描述中提取结构化要求，输出格式：
                    必要技能：[技能1, 技能2, ...]
                    经验要求：X年以上
                    学历要求：本科/硕士/博士
                    核心职责：[职责1, 职责2, ...]
                    加分项：[加分项1, 加分项2, ...]
                    
                    岗位描述：
                    %s""".formatted(jdText.length() > 3000 ? jdText.substring(0, 3000) : jdText);
            return deepSeekClient.evaluateResume(prompt);
        } catch (Exception e) {
            log.warn("JD requirement extraction failed: {}", e.getMessage());
            return "";
        }
    }

    /**
     * Get a summary of indexed JDs count for trace display.
     */
    public int getIndexedJdCount() {
        return jdMetaCache.size();
    }

    private record JdMeta(String jdId, String title, String category) {}
}
