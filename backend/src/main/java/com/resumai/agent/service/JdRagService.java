package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.resumai.agent.api.JdVersionConflictException;
import com.resumai.agent.api.dto.JdDetailResponse;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.api.dto.JdSummaryResponse;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.UpsertJdRequest;
import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.dao.JdLibraryMapper;
import com.resumai.agent.domain.entity.JdLibrary;
import com.resumai.agent.util.HrContext;
import dev.langchain4j.data.document.Metadata;
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
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
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
    private final JdLibraryMapper jdLibraryMapper;
    private final EmbeddingAvailability embeddingAvailability;
    private final MilvusVectorMaintenanceService vectorMaintenanceService;

    private final Map<String, JdMeta> jdMetaCache = new ConcurrentHashMap<>();

    private static final Map<String, List<String>> SKILL_SYNONYMS = Map.ofEntries(
            Map.entry("java", List.of("java", "jdk", "jvm")),
            Map.entry("spring", List.of("spring", "springboot", "spring boot", "spring-boot")),
            Map.entry("mysql", List.of("mysql", "mariadb")),
            Map.entry("redis", List.of("redis")),
            Map.entry("docker", List.of("docker", "k8s", "kubernetes", "容器")),
            Map.entry("rag", List.of("rag", "检索增强", "向量检索", "embedding", "milvus")),
            Map.entry("agent", List.of("agent", "智能体", "multi-agent", "agentops")),
            Map.entry("llm", List.of("llm", "大模型", "deepseek", "gpt", "chatgpt", "模型平台", "方舟")),
            Map.entry("trace", List.of("trace", "tracing", "可观测", "opentelemetry", "日志对账", "链路追踪")),
            Map.entry("backend", List.of("backend", "后端", "服务端"))
    );

    private static final Set<String> NON_SKILL_GAP_WORDS = Set.of(
            "招聘", "hr", "面试", "工程师", "经理", "负责", "要求", "熟悉", "具备", "以上", "本科", "硕士",
            "必要技能", "经验", "经验要求", "学历要求", "核心职责", "加分项", "年以上", "年", "技能"
    );

    /** 简历时间线区间：2020.01-2023.06 / 2020年1月-至今 / 2020-2024 */
    private static final Pattern TIMELINE_RANGE = Pattern.compile(
            "(20\\d{2}|19\\d{2})(?:\\s*[./年]\\s*(\\d{1,2})\\s*月?)?\\s*[-–—~至到]+\\s*"
                    + "(?:((?:20\\d{2}|19\\d{2})(?:\\s*[./年]\\s*(\\d{1,2})\\s*月?)?)|至今|现在|now|present)",
            Pattern.CASE_INSENSITIVE);

    /** 「必要技能」段结束于下一结构化字段 */
    private static final Pattern REQUIRED_SKILL_SECTION_END = Pattern.compile(
            "(?:经验要求|学历要求|核心职责|加分项|岗位职责|任职要求|工作职责)[：:]");
    private static final Pattern JD_SECTION_LABEL = Pattern.compile(
            "(?<![A-Za-z0-9_\\u3400-\\u9fff])"
                    + "(岗位职责|工作职责|职位职责|工作内容|岗位描述|任职要求|职位要求|"
                    + "必须技能|必要技能|技能要求|加分项|经验要求|生产场景(?:与考核题)?)\\s*[：:]",
            Pattern.CASE_INSENSITIVE);


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

    public JdRagService(@Qualifier("jdEmbeddingStore") @org.springframework.lang.Nullable MilvusEmbeddingStore jdEmbeddingStore,
                        @Qualifier("jdEmbeddingModel") EmbeddingModel embeddingModel,
                        JdLibraryMapper jdLibraryMapper,
                        EmbeddingAvailability embeddingAvailability,
                        MilvusVectorMaintenanceService vectorMaintenanceService) {
        this.jdEmbeddingStore = jdEmbeddingStore;
        this.embeddingModel = embeddingModel;
        this.jdLibraryMapper = jdLibraryMapper;
        this.embeddingAvailability = embeddingAvailability;
        this.vectorMaintenanceService = vectorMaintenanceService;
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
        upsertJdInternal(jdId, title, category, jdText, null, HrContext.getHrId(), true);
    }

    public JdDetailResponse createJd(UpsertJdRequest request) {
        String jdId = StringUtils.hasText(request.jdId()) ? request.jdId() : "jd-" + System.currentTimeMillis();
        upsertJdInternal(jdId, request.title(), request.category(), request.description(), null, HrContext.getHrId(), true);
        return getJdDetail(jdId);
    }

    public JdDetailResponse updateJd(String jdId, UpsertJdRequest request) {
        if (request.version() == null) {
            throw new IllegalArgumentException("更新 JD 必须提供 version");
        }
        upsertJdInternal(jdId, request.title(), request.category(), request.description(), request.version(), HrContext.getHrId(), false);
        return getJdDetail(jdId);
    }

    private void upsertJdInternal(String jdId, String title, String category, String jdText,
                                  Integer expectedVersion, String updatedBy, boolean allowInsert) {
        if (!StringUtils.hasText(jdText)) {
            return;
        }
        JdLibrary existing = jdLibraryMapper.selectOne(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
        int newVersion;
        if (existing == null) {
            if (!allowInsert) {
                throw new IllegalArgumentException("岗位不存在：" + jdId);
            }
            JdLibrary entity = new JdLibrary();
            entity.setJdId(jdId);
            entity.setTitle(title);
            entity.setCategory(category);
            entity.setDescription(jdText);
            entity.setVersion(1);
            entity.setUpdatedBy(updatedBy);
            entity.setTenantId("default");
            entity.setCreateTime(LocalDateTime.now());
            entity.setUpdateTime(LocalDateTime.now());
            entity.setDeleted(0);
            jdLibraryMapper.insert(entity);
            newVersion = 1;
        } else {
            if (expectedVersion != null && (existing.getVersion() == null || !expectedVersion.equals(existing.getVersion()))) {
                throw new JdVersionConflictException(toDetail(existing));
            }
            int currentVersion = existing.getVersion() != null ? existing.getVersion() : 1;
            newVersion = expectedVersion != null ? currentVersion + 1 : currentVersion;
            com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper<JdLibrary> updateWrapper =
                    new com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper<JdLibrary>()
                            .eq(JdLibrary::getJdId, jdId)
                            .set(JdLibrary::getTitle, title)
                            .set(JdLibrary::getCategory, category)
                            .set(JdLibrary::getDescription, jdText)
                            .set(JdLibrary::getUpdatedBy, updatedBy)
                            .set(JdLibrary::getUpdateTime, LocalDateTime.now());
            if (expectedVersion != null) {
                updateWrapper.eq(JdLibrary::getVersion, expectedVersion)
                        .set(JdLibrary::getVersion, newVersion);
            }
            int updated = jdLibraryMapper.update(null, updateWrapper);
            if (expectedVersion != null && updated == 0) {
                JdLibrary latest = jdLibraryMapper.selectOne(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
                throw new JdVersionConflictException(toDetail(latest));
            }
            if (expectedVersion == null) {
                newVersion = currentVersion;
            }
        }
        jdMetaCache.put(jdId, new JdMeta(jdId, title, category));
        reindexVectors(jdId, title, category, jdText, newVersion);
    }

    private int reindexVectors(String jdId, String title, String category, String jdText, int jdVersion) {
        try {
            if (!embeddingAvailability.isOperational()) {
                log.info("JD '{}' persisted to DB; Milvus index skipped ({})", title, embeddingAvailability.disabledReason());
                return 0;
            }
            vectorMaintenanceService.deleteJdVectors(jdId);
            String fullText = "岗位: " + title + "\n类别: " + category + "\n" + jdText;
            Metadata metadata = Metadata.from(Map.of(
                    "jdId", jdId,
                    "title", title,
                    "category", category,
                    "jdVersion", String.valueOf(jdVersion)
            ));
            List<TextSegment> segments = sectionPrefixSegments(title, fullText, metadata);
            List<Embedding> embeddings = embeddingModel.embedAll(segments).content();
            jdEmbeddingStore.addAll(embeddings, segments);
            log.info("Indexed JD '{}' v{} ({} chunks)", title, jdVersion, segments.size());
            return segments.size();
        } catch (Exception e) {
            log.warn("Failed to index JD '{}': {}", title, e.getMessage());
            return 0;
        }
    }

    /** Rebuild every persisted JD into the isolated TE3-768 winner collection. */
    public Map<String, Object> reindexAllJds() {
        List<JdLibrary> rows = getAllJds();
        int chunks = 0;
        int indexed = 0;
        for (JdLibrary row : rows) {
            if (row == null || !StringUtils.hasText(row.getJdId())
                    || !StringUtils.hasText(row.getDescription())) {
                continue;
            }
            int count = reindexVectors(
                    row.getJdId(), row.getTitle(), row.getCategory(), row.getDescription(),
                    row.getVersion() == null ? 1 : row.getVersion());
            if (count > 0) {
                indexed++;
                chunks += count;
            }
        }
        return Map.of(
                "documents", rows.size(),
                "indexedDocuments", indexed,
                "indexedChunks", chunks,
                "chunkStrategy", "section_prefix",
                "chunkSize", 400,
                "chunkOverlap", 40,
                "embeddingDimension", 768,
                "status", indexed == rows.size() ? "ready" : "degraded");
    }

    private List<TextSegment> sectionPrefixSegments(
            String title, String text, Metadata metadata) {
        record Section(String title, String body) {}
        List<Section> sections = new ArrayList<>();
        Matcher matcher = JD_SECTION_LABEL.matcher(text);
        List<Integer> starts = new ArrayList<>();
        List<String> labels = new ArrayList<>();
        while (matcher.find()) {
            starts.add(matcher.start(1));
            labels.add(matcher.group(1));
        }
        if (starts.isEmpty()) {
            sections.add(new Section(title, text));
        } else {
            if (starts.get(0) > 0 && StringUtils.hasText(text.substring(0, starts.get(0)))) {
                sections.add(new Section(title, text.substring(0, starts.get(0)).trim()));
            }
            for (int i = 0; i < starts.size(); i++) {
                int end = i + 1 < starts.size() ? starts.get(i + 1) : text.length();
                sections.add(new Section(labels.get(i), text.substring(starts.get(i), end).trim()));
            }
        }

        List<TextSegment> segments = new ArrayList<>();
        for (Section section : sections) {
            if (!StringUtils.hasText(section.body())) continue;
            String prefix = "文档：" + title + "\n章节：" + section.title() + "\n";
            int window = Math.max(80, 400 - prefix.length());
            for (String piece : smartWindows(section.body(), window, 40)) {
                segments.add(TextSegment.from(prefix + piece, metadata));
            }
        }
        return segments;
    }

    private List<String> smartWindows(String text, int size, int overlap) {
        if (!StringUtils.hasText(text)) return List.of();
        if (text.length() <= size) return List.of(text.trim());
        List<String> windows = new ArrayList<>();
        int start = 0;
        String[] boundaries = {"\n\n", "\n", "。", "；", ";", "，", ",", " "};
        while (start < text.length()) {
            int target = Math.min(text.length(), start + size);
            int end = target;
            if (target < text.length()) {
                int floor = start + Math.max(80, (int) (size * 0.55));
                int best = -1;
                for (String boundary : boundaries) {
                    int found = text.lastIndexOf(boundary, target - 1);
                    if (found >= floor) best = Math.max(best, found + boundary.length());
                }
                if (best > floor) end = best;
            }
            String piece = text.substring(start, end).trim();
            if (StringUtils.hasText(piece)) windows.add(piece);
            if (end >= text.length()) break;
            start = Math.max(start + 1, end - Math.min(overlap, size - 1));
        }
        return windows;
    }

    private JdDetailResponse toDetail(JdLibrary row) {
        return new JdDetailResponse(
                row.getJdId(), row.getTitle(), row.getCategory(), row.getDescription(),
                row.getVersion(), row.getUpdatedBy(), row.getTenantId(),
                row.getCreateTime(), row.getUpdateTime()
        );
    }

    public List<JdMatchResult> matchTopJds(String resumeText, int topK) {
        return matchTopJds(resumeText, topK, null);
    }

    public List<JdMatchResult> matchTopJds(String resumeText, int topK, com.resumai.agent.rag.RagOptions options) {
        if (!StringUtils.hasText(resumeText)) return List.of();
        ensureDefaultJdsSeeded();
        com.resumai.agent.rag.RagOptions opts = options != null ? options : com.resumai.agent.rag.RagOptions.defaults();
        int effectiveTopK = topK > 0 ? topK : opts.topK();
        if (embeddingAvailability.isOperational() && !"lexical".equals(opts.strategy())) {
            List<JdMatchResult> vectorMatches = matchTopJdsViaVector(resumeText, effectiveTopK, opts);
            if (!vectorMatches.isEmpty()) {
                return vectorMatches;
            }
        }
        return matchTopJdsViaLexical(resumeText, effectiveTopK);
    }

    List<JdMatchResult> matchTopJdsViaVector(String resumeText, int topK, com.resumai.agent.rag.RagOptions opts) {
        return matchTopJdsViaVectorInternal(resumeText, topK, opts);
    }

    private List<JdMatchResult> matchTopJdsViaVectorInternal(String resumeText, int topK, com.resumai.agent.rag.RagOptions opts) {
        if (!embeddingAvailability.isOperational()) {
            return List.of();
        }
        try {
            String queryText = resumeText.length() > 2000 ? resumeText.substring(0, 2000) : resumeText;
            Embedding queryEmbedding = embeddingModel.embed(queryText).content();
            double minScore = opts != null ? opts.scoreThreshold() : 0.35;
            EmbeddingSearchRequest request = EmbeddingSearchRequest.builder()
                    .queryEmbedding(queryEmbedding)
                    .maxResults(topK * 3)
                    .minScore(minScore)
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
                JdLibrary row = jdLibraryMapper.selectOne(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
                if (row != null && row.getVersion() != null) {
                    String versionText = seg.metadata().getString("jdVersion");
                    if (StringUtils.hasText(versionText)) {
                        try {
                            int segVersion = Integer.parseInt(versionText);
                            if (segVersion < row.getVersion()) {
                                continue;
                            }
                        } catch (NumberFormatException ignored) {
                            // keep legacy vectors without version metadata
                        }
                    }
                }
                if (!deduped.containsKey(jdId)) {
                    deduped.put(jdId, enrichMatchResult(jdId, title, category, match.score(), resumeText, "vector"));
                }
                if (deduped.size() >= topK) break;
            }
            return new ArrayList<>(deduped.values());
        } catch (Exception e) {
            log.warn("JD vector matching failed: {}", e.getMessage());
            return List.of();
        }
    }

    private List<JdLibrary> loadJdsForMatching() {
        List<JdLibrary> fromDb = getAllJds();
        if (!fromDb.isEmpty()) {
            return fromDb;
        }
        log.warn("JD library empty in DB, using built-in default JD catalog for lexical matching");
        List<JdLibrary> defaults = new ArrayList<>();
        for (DefaultJd jd : DEFAULT_JDS) {
            JdLibrary entity = new JdLibrary();
            entity.setJdId(jd.jdId());
            entity.setTitle(jd.title());
            entity.setCategory(jd.category());
            entity.setDescription(jd.description());
            defaults.add(entity);
        }
        return defaults;
    }

    /**
     * 词面路召回：标准 BM25（k1=1.2, b=0.75）。查询=简历分词后的词袋，
     * 文档=JD（标题+类别+描述），df/avgdl 每次查询在 JD 全量语料上即时统计
     * （JD 库量级为十~百级，统计成本微秒级，且天然随增删保持新鲜，
     * 无需缓存失效逻辑）。分词：英文按词、中文按 bigram。
     */
    // EXP-3: BM25 参数 k1/b 待检索实验校准
    private static final double BM25_K1 = 1.2;
    private static final double BM25_B = 0.75;

    List<JdMatchResult> matchTopJdsViaLexical(String resumeText, int topK) {
        List<JdLibrary> allJds = loadJdsForMatching();
        if (allJds.isEmpty()) {
            return List.of();
        }
        // 语料统计：每个 JD 的词频表与长度、全语料 df 与平均长度
        List<Map<String, Integer>> termFrequencies = new ArrayList<>(allJds.size());
        Map<String, Integer> documentFrequency = new HashMap<>();
        long totalLength = 0;
        for (JdLibrary jd : allJds) {
            String docText = (jd.getTitle() != null ? jd.getTitle() : "") + " "
                    + (jd.getCategory() != null ? jd.getCategory() : "") + " "
                    + (jd.getDescription() != null ? jd.getDescription() : "");
            Map<String, Integer> tf = new HashMap<>();
            for (String term : bm25Tokenize(docText)) {
                tf.merge(term, 1, Integer::sum);
            }
            termFrequencies.add(tf);
            totalLength += tf.values().stream().mapToInt(Integer::intValue).sum();
            for (String term : tf.keySet()) {
                documentFrequency.merge(term, 1, Integer::sum);
            }
        }
        int corpusSize = allJds.size();
        double avgdl = Math.max(1.0, (double) totalLength / corpusSize);

        // 查询词袋（简历截前 3000 字符足够覆盖技能与近期经历）
        String queryText = resumeText.length() > 3000 ? resumeText.substring(0, 3000) : resumeText;
        Set<String> queryTerms = new LinkedHashSet<>(bm25Tokenize(queryText));
        if (queryTerms.isEmpty()) {
            return List.of();
        }

        List<JdMatchResult> scored = new ArrayList<>();
        double maxScore = 0;
        double[] rawScores = new double[corpusSize];
        for (int i = 0; i < corpusSize; i++) {
            Map<String, Integer> tf = termFrequencies.get(i);
            double docLength = tf.values().stream().mapToInt(Integer::intValue).sum();
            double score = 0;
            for (String term : queryTerms) {
                Integer frequency = tf.get(term);
                if (frequency == null) {
                    continue;
                }
                int df = documentFrequency.getOrDefault(term, 1);
                double idf = Math.log(1 + (corpusSize - df + 0.5) / (df + 0.5));
                double numerator = frequency * (BM25_K1 + 1);
                double denominator = frequency
                        + BM25_K1 * (1 - BM25_B + BM25_B * docLength / avgdl);
                score += idf * numerator / denominator;
            }
            rawScores[i] = score;
            maxScore = Math.max(maxScore, score);
        }
        for (int i = 0; i < corpusSize; i++) {
            if (rawScores[i] <= 0) {
                continue;
            }
            JdLibrary jd = allJds.get(i);
            // 归一化到 (0,1]，保持与向量路分数同一量纲供 RRF/展示使用
            double normalized = maxScore > 0 ? rawScores[i] / maxScore : 0;
            scored.add(enrichMatchResult(jd.getJdId(), jd.getTitle(), jd.getCategory(),
                    Math.min(0.95, 0.15 + normalized * 0.8), resumeText, "bm25"));
        }
        scored.sort((a, b) -> Double.compare(b.matchScore(), a.matchScore()));
        List<JdMatchResult> top = scored.stream().limit(topK).collect(Collectors.toList());
        return top;
    }

    /** BM25 分词：英文单词（含 +#. 的技术词）+ 中文 bigram。 */
    private List<String> bm25Tokenize(String text) {
        List<String> terms = new ArrayList<>();
        if (!StringUtils.hasText(text)) {
            return terms;
        }
        Matcher english = Pattern.compile("[a-zA-Z][a-zA-Z0-9+#\\.]*").matcher(text);
        while (english.find()) {
            String word = english.group().toLowerCase(Locale.ROOT);
            if (word.length() >= 2) {
                terms.add(word);
            }
        }
        Matcher cjk = Pattern.compile("[\\u4e00-\\u9fff]+").matcher(text);
        while (cjk.find()) {
            String run = cjk.group();
            if (run.length() == 1) {
                terms.add(run);
                continue;
            }
            for (int i = 0; i + 1 < run.length(); i++) {
                terms.add(run.substring(i, i + 2));
            }
        }
        return terms;
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

    private JdMatchResult enrichMatchResult(String jdId, String title, String category,
                                            double channelScore, String resumeText, String channel) {
        String jdText = loadJdDescription(jdId);
        DimensionalMatch dimensional = computeDimensionalMatch(resumeText, jdText);
        double blendedScore = dimensional.overallScore() > 0 ? dimensional.overallScore() : channelScore;
        List<String> reasons = buildMatchReasons(resumeText, jdText, title, dimensional);
        List<String> gaps = buildGaps(resumeText, jdText, dimensional);
        List<String> checks = buildInterviewChecks(gaps, title);
        Double vectorScore = "vector".equals(channel) ? channelScore : null;
        Double bm25Score = "bm25".equals(channel) ? channelScore : null;
        JdMatchResult base = new JdMatchResult(
                jdId,
                title != null ? title : "",
                category != null ? category : "TECH",
                blendedScore,
                reasons,
                gaps,
                checks,
                dimensional.skillMatchScore(),
                dimensional.experienceMatchScore(),
                dimensional.projectMatchScore(),
                dimensional.riskPenalty()
        );
        return base.withChannelScores(vectorScore, bm25Score).withProvenance(buildJdProvenance(jdId));
    }

    private com.resumai.agent.api.dto.RagProvenance buildJdProvenance(String jdId) {
        JdLibrary row = null;
        try {
            row = jdLibraryMapper.selectOne(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
        } catch (Exception ignored) {
        }
        if (row == null) {
            return com.resumai.agent.api.dto.RagProvenance.document(jdId, null, null, null);
        }
        return new com.resumai.agent.api.dto.RagProvenance(
                row.getJdId(),
                null,
                row.getVersion() != null ? String.valueOf(row.getVersion()) : null,
                row.getCreateTime() != null ? row.getCreateTime().toString() : null,
                row.getUpdateTime() != null ? row.getUpdateTime().toString() : null,
                null,
                null,
                null,
                null,
                null,
                null,
                null);
    }

    private record DimensionalMatch(
            double skillMatchScore,
            double experienceMatchScore,
            double projectMatchScore,
            double riskPenalty,
            double overallScore
    ) {}

    private DimensionalMatch computeDimensionalMatch(String resumeText, String jdText) {
        String resumeLower = resumeText.toLowerCase(Locale.ROOT);
        String jdLower = (jdText + " " + jdText).toLowerCase(Locale.ROOT);
        List<String> requiredSkills = extractRequiredSkills(jdText);
        if (requiredSkills.isEmpty()) {
            requiredSkills = extractKeywords(jdText).stream().limit(12).toList();
        }
        long skillHits = requiredSkills.stream().filter(skill -> matchesSynonym(resumeLower, skill)).count();
        double skillMatch = requiredSkills.isEmpty() ? 0.65
                : Math.min(0.98, (double) skillHits / requiredSkills.size());

        double experienceMatch = computeExperienceMatch(resumeLower, jdLower);
        double projectMatch = computeProjectMatch(resumeLower, jdLower);
        double riskPenalty = computeRiskPenalty(resumeLower, jdLower);

        double overall = Math.max(0.05, Math.min(0.98,
                skillMatch * 0.45 + experienceMatch * 0.25 + projectMatch * 0.20 - riskPenalty * 0.10));
        return new DimensionalMatch(skillMatch, experienceMatch, projectMatch, riskPenalty, overall);
    }

    private List<String> extractRequiredSkills(String jdText) {
        List<String> skills = new ArrayList<>();
        if (!StringUtils.hasText(jdText)) {
            return skills;
        }
        int idx = jdText.indexOf("必要技能");
        if (idx < 0) {
            return skills;
        }
        String after = jdText.substring(idx + "必要技能".length()).replaceFirst("^[：:\\s]+", "");
        Matcher end = REQUIRED_SKILL_SECTION_END.matcher(after);
        String segment = end.find() ? after.substring(0, end.start()) : after;
        // 防止无字段分隔时吞掉整段描述：最多截到首个句号/换行后的下一字段痕迹
        int hardCut = segment.length();
        Matcher periodCut = Pattern.compile("[。\\n]").matcher(segment);
        if (periodCut.find() && periodCut.start() + 1 < segment.length()) {
            String rest = segment.substring(periodCut.start() + 1);
            if (REQUIRED_SKILL_SECTION_END.matcher(rest).lookingAt()
                    || rest.startsWith("经验") || rest.startsWith("学历")) {
                hardCut = periodCut.start();
            }
        }
        segment = segment.substring(0, hardCut);

        Matcher matcher = Pattern.compile("[A-Za-z][A-Za-z0-9+#\\.\\-/]*|[\\u4e00-\\u9fa5]{2,8}").matcher(segment);
        while (matcher.find() && skills.size() < 16) {
            String token = matcher.group().trim();
            String lower = token.toLowerCase(Locale.ROOT);
            if (token.length() >= 2
                    && !NON_SKILL_GAP_WORDS.contains(lower)
                    && !lower.matches("\\d+")
                    && !lower.endsWith("年以上")
                    && !lower.contains("经验要求")) {
                skills.add(normalizeSkillToken(token));
            }
        }
        return skills.stream().distinct().limit(12).toList();
    }

    private String normalizeSkillToken(String token) {
        String lower = token.toLowerCase(Locale.ROOT);
        for (Map.Entry<String, List<String>> entry : SKILL_SYNONYMS.entrySet()) {
            if (entry.getValue().stream().anyMatch(v -> lower.contains(v) || v.contains(lower))) {
                return entry.getKey();
            }
        }
        return lower;
    }

    private boolean matchesSynonym(String resumeLower, String skillToken) {
        String normalized = normalizeSkillToken(skillToken);
        List<String> synonyms = SKILL_SYNONYMS.getOrDefault(normalized, List.of(normalized));
        return synonyms.stream().anyMatch(resumeLower::contains);
    }

    private double computeExperienceMatch(String resumeLower, String jdLower) {
        Matcher jdYears = Pattern.compile("(\\d+)\\s*年以上").matcher(jdLower);
        int requiredYears = jdYears.find() ? Integer.parseInt(jdYears.group(1)) : 3;

        double candidateYears = estimateYearsFromTimeline(resumeLower);
        if (candidateYears <= 0) {
            // 次选：明确「N年经验/工作」表述，避免把任意「N年」误当工龄
            Matcher resumeYears = Pattern.compile("(\\d+(?:\\.\\d+)?)\\s*年(?:以上)?(?:工作)?经验").matcher(resumeLower);
            while (resumeYears.find()) {
                candidateYears = Math.max(candidateYears, Double.parseDouble(resumeYears.group(1)));
            }
        }
        if (candidateYears <= 0) {
            // 无法从时间线或明确年限推断：给不确定中性分，不再硬编码 0.55
            return 0.40;
        }
        return Math.min(1.0, candidateYears / (double) Math.max(requiredYears, 1));
    }

    /**
     * 从履历时间线区间估算工作年限（月并集 / 12）。
     * 支持 2020.01-2023.06、2020年1月-至今、2020-2024。
     */
    private double estimateYearsFromTimeline(String resumeText) {
        if (!StringUtils.hasText(resumeText)) {
            return 0;
        }
        int nowYear = LocalDateTime.now().getYear();
        int nowMonth = LocalDateTime.now().getMonthValue();
        int nowIndex = nowYear * 12 + (nowMonth - 1);

        List<int[]> intervals = new ArrayList<>();
        Matcher matcher = TIMELINE_RANGE.matcher(resumeText);
        while (matcher.find()) {
            int startYear = Integer.parseInt(matcher.group(1));
            int startMonth = matcher.group(2) != null ? clampMonth(Integer.parseInt(matcher.group(2))) : 1;
            int startIndex = startYear * 12 + (startMonth - 1);

            int endIndex;
            String endToken = matcher.group(3);
            if (endToken == null || endToken.matches("(?i)至今|现在|now|present")) {
                endIndex = nowIndex;
            } else {
                Matcher endParts = Pattern.compile("(20\\d{2}|19\\d{2})(?:\\s*[./年]\\s*(\\d{1,2}))?").matcher(endToken);
                if (!endParts.find()) {
                    continue;
                }
                int endYear = Integer.parseInt(endParts.group(1));
                int endMonth = endParts.group(2) != null ? clampMonth(Integer.parseInt(endParts.group(2))) : 12;
                endIndex = endYear * 12 + (endMonth - 1);
            }
            if (endIndex < startIndex) {
                continue;
            }
            intervals.add(new int[]{startIndex, endIndex});
        }
        if (intervals.isEmpty()) {
            return 0;
        }
        intervals.sort(Comparator.comparingInt(a -> a[0]));
        int mergedMonths = 0;
        int curStart = intervals.get(0)[0];
        int curEnd = intervals.get(0)[1];
        for (int i = 1; i < intervals.size(); i++) {
            int[] next = intervals.get(i);
            if (next[0] <= curEnd + 1) {
                curEnd = Math.max(curEnd, next[1]);
            } else {
                mergedMonths += curEnd - curStart + 1;
                curStart = next[0];
                curEnd = next[1];
            }
        }
        mergedMonths += curEnd - curStart + 1;
        return Math.min(40.0, mergedMonths / 12.0);
    }

    private static int clampMonth(int month) {
        return Math.max(1, Math.min(12, month));
    }

    private double computeProjectMatch(String resumeLower, String jdLower) {
        List<String> projectSignals = List.of("项目", "重构", "平台", "系统", "agent", "rag", "trace", "微服务", "架构");
        long hits = projectSignals.stream().filter(s -> resumeLower.contains(s) && jdLower.contains(s)).count();
        return Math.min(0.95, 0.35 + hits * 0.12);
    }

    private double computeRiskPenalty(String resumeLower, String jdLower) {
        double penalty = 0;
        if (jdLower.contains("5年") && Pattern.compile("[12]\\s*年").matcher(resumeLower).find()) {
            penalty += 0.35;
        }
        if (resumeLower.contains("严重不符") || resumeLower.contains("空窗")) {
            penalty += 0.15;
        }
        return Math.min(0.5, penalty);
    }

    public String getJdDescription(String jdId) {
        return loadJdDescription(jdId);
    }

    private String loadJdDescription(String jdId) {
        try {
            JdLibrary jd = jdLibraryMapper.selectOne(
                    new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId).last("limit 1"));
            if (jd != null && jd.getDescription() != null) {
                return jd.getDescription();
            }
        } catch (Exception e) {
            log.debug("Load JD description from DB failed: {}", e.getMessage());
        }
        return DEFAULT_JDS.stream()
                .filter(d -> d.jdId().equals(jdId))
                .map(DefaultJd::description)
                .findFirst()
                .orElse("");
    }

    private List<String> buildMatchReasons(String resumeText, String jdText, String title, DimensionalMatch dimensional) {
        List<String> reasons = new ArrayList<>();
        String resumeLower = resumeText.toLowerCase(Locale.ROOT);
        for (String skill : extractRequiredSkills(jdText)) {
            if (matchesSynonym(resumeLower, skill) && reasons.size() < 4) {
                reasons.add("技能匹配：" + skill);
            }
        }
        reasons.add(String.format("技能维度 %.0f%%，经验维度 %.0f%%，项目维度 %.0f%%",
                dimensional.skillMatchScore() * 100,
                dimensional.experienceMatchScore() * 100,
                dimensional.projectMatchScore() * 100));
        if (reasons.size() == 1 && StringUtils.hasText(title)) {
            reasons.add("岗位「" + title + "」与简历整体语义相近");
        }
        return reasons;
    }

    private List<String> buildGaps(String resumeText, String jdText, DimensionalMatch dimensional) {
        List<String> gaps = new ArrayList<>();
        String resumeLower = resumeText.toLowerCase(Locale.ROOT);
        for (String skill : extractRequiredSkills(jdText)) {
            if (!matchesSynonym(resumeLower, skill)
                    && !NON_SKILL_GAP_WORDS.contains(skill.toLowerCase(Locale.ROOT))
                    && gaps.size() < 3) {
                gaps.add("未明确体现：" + skill);
            }
        }
        if (dimensional.experienceMatchScore() < 0.6) {
            gaps.add("经验年限与岗位要求存在差距");
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

    public int getIndexedJdCount() {
        try {
            Long count = jdLibraryMapper.selectCount(new LambdaQueryWrapper<JdLibrary>());
            return count != null ? count.intValue() : jdMetaCache.size();
        } catch (Exception e) {
            return jdMetaCache.size();
        }
    }

    public PageResult<JdSummaryResponse> queryJds(String keyword, String category, String sortBy, int page, int pageSize) {
        int safePage = Math.max(page, 1);
        int safeSize = Math.min(Math.max(pageSize, 1), 100);
        LambdaQueryWrapper<JdLibrary> wrapper = new LambdaQueryWrapper<>();
        if (StringUtils.hasText(keyword)) {
            String q = keyword.trim();
            wrapper.and(w -> w.like(JdLibrary::getTitle, q)
                    .or().like(JdLibrary::getCategory, q)
                    .or().like(JdLibrary::getJdId, q));
        }
        if (StringUtils.hasText(category) && !"ALL".equalsIgnoreCase(category)) {
            wrapper.eq(JdLibrary::getCategory, category.trim().toUpperCase());
        }
        if ("title".equalsIgnoreCase(sortBy)) {
            wrapper.orderByAsc(JdLibrary::getTitle);
        } else if ("category".equalsIgnoreCase(sortBy)) {
            wrapper.orderByAsc(JdLibrary::getCategory).orderByDesc(JdLibrary::getCreateTime);
        } else {
            wrapper.orderByDesc(JdLibrary::getCreateTime);
        }
        try {
            Page<JdLibrary> mpPage = jdLibraryMapper.selectPage(new Page<>(safePage, safeSize), wrapper);
            List<JdSummaryResponse> items = mpPage.getRecords().stream()
                    .map(row -> new JdSummaryResponse(row.getJdId(), row.getTitle(), row.getCategory(),
                            row.getCreateTime(), row.getUpdateTime()))
                    .toList();
            return PageResult.of(items, mpPage.getTotal(), safePage, safeSize);
        } catch (Exception e) {
            log.warn("Failed to query JDs from DB: {}", e.getMessage());
            return PageResult.of(List.of(), 0, safePage, safeSize);
        }
    }

    public JdDetailResponse getJdDetail(String jdId) {
        if (!StringUtils.hasText(jdId)) {
            throw new IllegalArgumentException("jdId 不能为空");
        }
        JdLibrary row = jdLibraryMapper.selectOne(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
        if (row == null) {
            throw new IllegalArgumentException("岗位不存在：" + jdId);
        }
        return new JdDetailResponse(row.getJdId(), row.getTitle(), row.getCategory(), row.getDescription(),
                row.getVersion(), row.getUpdatedBy(), row.getTenantId(),
                row.getCreateTime(), row.getUpdateTime());
    }

    public List<JdLibrary> getAllJds() {
        try {
            return jdLibraryMapper.selectList(new LambdaQueryWrapper<JdLibrary>().orderByDesc(JdLibrary::getCreateTime));
        } catch (Exception e) {
            log.warn("Failed to load JDs from DB: {}", e.getMessage());
            return List.of();
        }
    }

    public void deleteJd(String jdId) {
        if (!StringUtils.hasText(jdId)) {
            return;
        }
        try {
            jdLibraryMapper.delete(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getJdId, jdId));
            jdMetaCache.remove(jdId);
            vectorMaintenanceService.deleteJdVectors(jdId);
        } catch (Exception e) {
            log.warn("Failed to delete JD '{}': {}", jdId, e.getMessage());
        }
    }

    private record JdMeta(String jdId, String title, String category) {}
}
