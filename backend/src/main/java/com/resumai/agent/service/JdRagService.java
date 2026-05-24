package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.dao.JdLibraryMapper;
import com.resumai.agent.domain.entity.JdLibrary;
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

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

@Service
public class JdRagService {

    private static final Logger log = LoggerFactory.getLogger(JdRagService.class);

    private final MilvusEmbeddingStore jdEmbeddingStore;
    private final EmbeddingModel embeddingModel;
    private final DeepSeekClient deepSeekClient;
    private final JdLibraryMapper jdLibraryMapper;

    private final Map<String, JdMeta> jdMetaCache = new ConcurrentHashMap<>();

    public JdRagService(@Qualifier("jdEmbeddingStore") MilvusEmbeddingStore jdEmbeddingStore,
                        EmbeddingModel embeddingModel,
                        DeepSeekClient deepSeekClient,
                        JdLibraryMapper jdLibraryMapper) {
        this.jdEmbeddingStore = jdEmbeddingStore;
        this.embeddingModel = embeddingModel;
        this.deepSeekClient = deepSeekClient;
        this.jdLibraryMapper = jdLibraryMapper;
    }

    public void indexJd(String jdId, String title, String category, String jdText) {
        if (!StringUtils.hasText(jdText)) return;
        try {
            jdMetaCache.put(jdId, new JdMeta(jdId, title, category));

            persistJdToDb(jdId, title, category, jdText);

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

    private void persistJdToDb(String jdId, String title, String category, String description) {
        try {
            Long existing = jdLibraryMapper.selectCount(
                    new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
            if (existing != null && existing > 0) {
                JdLibrary update = new JdLibrary();
                update.setTitle(title);
                update.setCategory(category);
                update.setDescription(description);
                update.setUpdateTime(LocalDateTime.now());
                jdLibraryMapper.update(update, new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
            } else {
                JdLibrary entity = new JdLibrary();
                entity.setJdId(jdId);
                entity.setTitle(title);
                entity.setCategory(category);
                entity.setDescription(description);
                entity.setCreateTime(LocalDateTime.now());
                entity.setUpdateTime(LocalDateTime.now());
                entity.setDeleted(0);
                jdLibraryMapper.insert(entity);
            }
        } catch (Exception e) {
            log.warn("Failed to persist JD '{}' to MySQL: {}", jdId, e.getMessage());
        }
    }

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

    public int getIndexedJdCount() {
        try {
            Long count = jdLibraryMapper.selectCount(new LambdaQueryWrapper<JdLibrary>());
            return count != null ? count.intValue() : jdMetaCache.size();
        } catch (Exception e) {
            return jdMetaCache.size();
        }
    }

    public List<JdLibrary> getAllJds() {
        try {
            return jdLibraryMapper.selectList(new LambdaQueryWrapper<JdLibrary>().orderByDesc(JdLibrary::getCreateTime));
        } catch (Exception e) {
            log.warn("Failed to load JDs from DB: {}", e.getMessage());
            return List.of();
        }
    }

    private record JdMeta(String jdId, String title, String category) {}
}
