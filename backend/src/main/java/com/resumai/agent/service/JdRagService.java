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

import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

@Service
public class JdRagService {

    private static final Logger log = LoggerFactory.getLogger(JdRagService.class);

    private final MilvusEmbeddingStore jdEmbeddingStore;
    private final EmbeddingModel embeddingModel;
    private final DeepSeekClient deepSeekClient;
    private final JdLibraryMapper jdLibraryMapper;

    private final Map<String, JdMeta> jdMetaCache = new ConcurrentHashMap<>();

    private static final List<DefaultJd> DEFAULT_JDS = List.of(
            new DefaultJd("job-java-agent", "高级 Java / AI Agent 平台工程师", "TECH",
                    "招聘 Java 21 / Spring Boot 3 / AI Agent 平台方向高级后端工程师，要求熟悉 RAG、Trace 可观测、Docker 部署、线上问题排查和端到端交付。必要技能：Java, Spring Boot, MySQL, Redis, Docker, RAG, LLM。经验要求：5年以上。"),
            new DefaultJd("job-product-ai", "AI 产品经理", "PRODUCT",
                    "负责 AI 招聘产品从需求洞察、PRD、数据指标到上线迭代，要求理解 LLM/RAG 基础能力、B 端工作流和招聘业务。必要技能：产品设计, 数据分析, LLM, RAG, 招聘业务。"),
            new DefaultJd("job-frontend", "高级前端工程师", "TECH",
                    "负责 Vue3/React 前端架构、组件库、性能优化与可视化大屏。必要技能：Vue, TypeScript, CSS, 数据可视化, ECharts。"),
            new DefaultJd("job-data", "数据工程师", "TECH",
                    "负责数据采集、ETL、数仓建模与指标体系建设。必要技能：Python, SQL, Spark, Flink, 数据建模。")
    );

    private record DefaultJd(String jdId, String title, String category, String description) {}

    public JdRagService(@Qualifier("jdEmbeddingStore") MilvusEmbeddingStore jdEmbeddingStore,
                        EmbeddingModel embeddingModel,
                        DeepSeekClient deepSeekClient,
                        JdLibraryMapper jdLibraryMapper) {
        this.jdEmbeddingStore = jdEmbeddingStore;
        this.embeddingModel = embeddingModel;
        this.deepSeekClient = deepSeekClient;
        this.jdLibraryMapper = jdLibraryMapper;
    }

    public int ensureDefaultJdsSeeded() {
        int seeded = 0;
        for (DefaultJd jd : DEFAULT_JDS) {
            try {
                Long count = jdLibraryMapper.selectCount(
                        new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jd.jdId()));
                if (count == null || count == 0) {
                    seeded++;
                }
                indexJd(jd.jdId(), jd.title(), jd.category(), jd.description());
            } catch (Exception e) {
                log.warn("Seed JD '{}' failed: {}", jd.title(), e.getMessage());
            }
        }
        return seeded;
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
        ensureDefaultJdsSeeded();
        List<JdMatchResult> vectorMatches = matchTopJdsViaVector(resumeText, topK);
        if (!vectorMatches.isEmpty()) {
            return vectorMatches;
        }
        return matchTopJdsViaLexical(resumeText, topK);
    }

    private List<JdMatchResult> matchTopJdsViaVector(String resumeText, int topK) {
        try {
            String queryText = resumeText.length() > 2000 ? resumeText.substring(0, 2000) : resumeText;
            Embedding queryEmbedding = embeddingModel.embed(queryText).content();
            EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                    .queryEmbedding(queryEmbedding)
                    .maxResults(topK * 3)
                    .minScore(0.35)
                    .build();
            EmbeddingSearchResult<TextSegment> result = jdEmbeddingStore.search(request);
            List<EmbeddingMatch<TextSegment>> matches = result.matches();

            Map<String, JdMatchResult> deduped = new LinkedHashMap<>();
            for (EmbeddingMatch<TextSegment> match : matches) {
                TextSegment seg = match.embedded();
                if (seg == null || seg.metadata() == null) continue;
                String jdId = seg.metadata().getString("jdId");
                String title = seg.metadata().getString("title");
                String category = seg.metadata().getString("category");
                if (jdId == null) continue;
                if (!deduped.containsKey(jdId)) {
                    deduped.put(jdId, enrichMatchResult(jdId, title, category, match.score(), resumeText));
                }
                if (deduped.size() >= topK) break;
            }
            return new ArrayList<>(deduped.values());
        } catch (Exception e) {
            log.warn("JD vector matching failed: {}", e.getMessage());
            return List.of();
        }
    }

    private List<JdMatchResult> matchTopJdsViaLexical(String resumeText, int topK) {
        List<JdLibrary> allJds = getAllJds();
        if (allJds.isEmpty()) {
            ensureDefaultJdsSeeded();
            allJds = getAllJds();
        }
        if (allJds.isEmpty()) {
            return List.of();
        }
        String resumeLower = resumeText.toLowerCase(Locale.ROOT);
        List<JdMatchResult> scored = new ArrayList<>();
        for (JdLibrary jd : allJds) {
            String desc = jd.getDescription() != null ? jd.getDescription() : "";
            double score = lexicalScore(resumeLower, desc, jd.getTitle(), jd.getCategory());
            if (score > 0.08) {
                scored.add(enrichMatchResult(jd.getJdId(), jd.getTitle(), jd.getCategory(), score, resumeText));
            }
        }
        scored.sort((a, b) -> Double.compare(b.score(), a.score()));
        return scored.stream().limit(topK).collect(Collectors.toList());
    }

    private Set<String> extractKeywords(String text) {
        Set<String> keywords = new LinkedHashSet<>();
        if (!StringUtils.hasText(text)) {
            return keywords;
        }
        Matcher english = Pattern.compile("[a-zA-Z][a-zA-Z0-9+#\\.]*").matcher(text);
        while (english.find()) {
            String word = english.group().toLowerCase(Locale.ROOT);
            if (word.length() >= 2) {
                keywords.add(word);
            }
        }
        String lower = text.toLowerCase(Locale.ROOT);
        for (String segment : lower.split("[\\s,，、/|；;：:\\.\\(\\)（）\\[\\]{}\"'\\n]+")) {
            if (segment.length() >= 2 && !segment.matches("\\d+")) {
                keywords.add(segment);
            }
        }
        for (String tech : List.of("java", "spring", "mysql", "redis", "docker", "rag", "agent", "vue", "python", "sql", "backend", "后端", "工程师", "产品经理")) {
            if (lower.contains(tech)) {
                keywords.add(tech);
            }
        }
        return keywords.stream().limit(50).collect(Collectors.toCollection(LinkedHashSet::new));
    }

    private double lexicalScore(String resumeLower, String jdText, String title, String category) {
        Set<String> keywords = extractKeywords(jdText + " " + title + " " + category);
        if (keywords.isEmpty()) return 0.15;
        long hits = keywords.stream().filter(kw -> resumeLower.contains(kw.toLowerCase(Locale.ROOT))).count();
        double ratio = (double) hits / keywords.size();
        return Math.min(0.95, 0.15 + ratio * 0.8);
    }

    private JdMatchResult enrichMatchResult(String jdId, String title, String category, double score, String resumeText) {
        String jdText = loadJdDescription(jdId);
        List<String> reasons = buildMatchReasons(resumeText, jdText, title);
        List<String> gaps = buildGaps(resumeText, jdText);
        List<String> checks = buildInterviewChecks(gaps, title);
        return new JdMatchResult(
                jdId,
                title != null ? title : "",
                category != null ? category : "TECH",
                score,
                reasons,
                gaps,
                checks
        );
    }

    public String getJdDescription(String jdId) {
        return loadJdDescription(jdId);
    }

    private String loadJdDescription(String jdId) {
        try {
            JdLibrary jd = jdLibraryMapper.selectOne(
                    new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId).last("limit 1"));
            return jd != null && jd.getDescription() != null ? jd.getDescription() : "";
        } catch (Exception e) {
            return "";
        }
    }

    private List<String> buildMatchReasons(String resumeText, String jdText, String title) {
        List<String> reasons = new ArrayList<>();
        String resumeLower = resumeText.toLowerCase(Locale.ROOT);
        for (String kw : extractKeywords(jdText)) {
            if (resumeLower.contains(kw) && reasons.size() < 4) {
                reasons.add("简历包含岗位关键词：" + kw);
            }
        }
        if (reasons.isEmpty() && StringUtils.hasText(title)) {
            reasons.add("岗位「" + title + "」与简历整体语义相近");
        }
        return reasons;
    }

    private List<String> buildGaps(String resumeText, String jdText) {
        List<String> gaps = new ArrayList<>();
        String resumeLower = resumeText.toLowerCase(Locale.ROOT);
        for (String kw : extractKeywords(jdText)) {
            if (!resumeLower.contains(kw) && gaps.size() < 3) {
                gaps.add("未明确体现：" + kw);
            }
        }
        return gaps;
    }

    private List<String> buildInterviewChecks(List<String> gaps, String title) {
        List<String> checks = new ArrayList<>();
        for (String gap : gaps) {
            checks.add("请结合「" + title + "」说明 " + gap.replace("未明确体现：", "") + " 的实际经验");
        }
        if (checks.isEmpty()) {
            checks.add("请说明与目标岗位最匹配的一段项目经历");
        }
        return checks;
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
