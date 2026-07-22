package com.resumai.agent.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.config.EmbeddingProperties;
import com.resumai.agent.dao.JdLibraryMapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.JdLibrary;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.rag.RagOptions;
import com.resumai.agent.service.HybridRagService;
import com.resumai.agent.service.AgentMemoryService;
import com.resumai.agent.service.KnowledgeBaseDocumentService;
import com.resumai.agent.service.RagAdvisorService;
import com.resumai.agent.service.RagConfigService;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.core.io.ClassPathResource;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api/rag")
public class RagController {

    private final RagConfigService ragConfigService;
    private final HybridRagService hybridRagService;
    private final RagAdvisorService ragAdvisorService;
    private final EmbeddingAvailability embeddingAvailability;
    private final EmbeddingProperties embeddingProperties;
    private final ObjectMapper objectMapper;
    private final JdLibraryMapper jdLibraryMapper;
    private final ResumeTaskMapper resumeTaskMapper;
    private final KnowledgeBaseDocumentService knowledgeBaseDocumentService;
    private final AgentMemoryService agentMemoryService;

    public RagController(RagConfigService ragConfigService,
                         HybridRagService hybridRagService,
                         RagAdvisorService ragAdvisorService,
                         EmbeddingAvailability embeddingAvailability,
                         EmbeddingProperties embeddingProperties,
                         ObjectMapper objectMapper,
                         JdLibraryMapper jdLibraryMapper,
                         ResumeTaskMapper resumeTaskMapper,
                         KnowledgeBaseDocumentService knowledgeBaseDocumentService,
                         AgentMemoryService agentMemoryService) {
        this.ragConfigService = ragConfigService;
        this.hybridRagService = hybridRagService;
        this.ragAdvisorService = ragAdvisorService;
        this.embeddingAvailability = embeddingAvailability;
        this.embeddingProperties = embeddingProperties;
        this.objectMapper = objectMapper;
        this.jdLibraryMapper = jdLibraryMapper;
        this.resumeTaskMapper = resumeTaskMapper;
        this.knowledgeBaseDocumentService = knowledgeBaseDocumentService;
        this.agentMemoryService = agentMemoryService;
    }

    @GetMapping("/config")
    public Map<String, Object> getConfig() {
        RagOptions options = ragConfigService.getDefaultOptions();
        return Map.of(
                "options", options,
                "embeddingOperational", embeddingAvailability.isOperational(),
                "embeddingProvider", embeddingProperties.getProvider(),
                "presets", loadPresets());
    }

    @PutMapping("/config")
    public Map<String, Object> saveConfig(@RequestBody RagOptions options) {
        RagOptions saved = ragConfigService.saveDefaultOptions(options);
        return Map.of("options", saved);
    }

    @PostMapping("/preview")
    public Map<String, Object> preview(@RequestBody PreviewRequest request) {
        RagOptions opts = request.options() != null ? request.options() : ragConfigService.getDefaultOptions();
        return hybridRagService.preview(request.resumeText(), opts);
    }

    @PostMapping("/compare")
    public Map<String, Object> compare(@RequestBody CompareRequest request) {
        List<HybridRagService.NamedVariant> variants = request.variants().stream()
                .map(v -> new HybridRagService.NamedVariant(v.name(), v.options()))
                .toList();
        return Map.of("variants", hybridRagService.compare(request.resumeText(), variants));
    }

    @GetMapping("/advisor")
    public Map<String, Object> advisor() {
        return ragAdvisorService.suggest();
    }

    @GetMapping("/presets")
    public Map<String, Object> presets() {
        return Map.of("presets", loadPresets());
    }

    @GetMapping("/knowledge-base")
    public Map<String, Object> knowledgeBase() {
        long jdCount = jdLibraryMapper.selectCount(new LambdaQueryWrapper<JdLibrary>().eq(JdLibrary::getDeleted, 0));
        long taskCount = resumeTaskMapper.selectCount(new LambdaQueryWrapper<ResumeTask>().eq(ResumeTask::getDeleted, 0));
        long pdfCount = resumeTaskMapper.selectCount(new LambdaQueryWrapper<ResumeTask>()
                .eq(ResumeTask::getDeleted, 0)
                .like(ResumeTask::getFileName, ".pdf"));
        var recentJds = jdLibraryMapper.selectList(new LambdaQueryWrapper<JdLibrary>()
                        .eq(JdLibrary::getDeleted, 0)
                        .orderByDesc(JdLibrary::getUpdateTime)
                        .last("limit 5"))
                .stream()
                .map(jd -> Map.of(
                        "jdId", jd.getJdId() == null ? "" : jd.getJdId(),
                        "title", jd.getTitle() == null ? "" : jd.getTitle(),
                        "category", jd.getCategory() == null ? "" : jd.getCategory(),
                        "version", jd.getVersion()))
                .toList();
        var recentResumeRows = resumeTaskMapper.selectList(new LambdaQueryWrapper<ResumeTask>()
                        .eq(ResumeTask::getDeleted, 0)
                        .orderByDesc(ResumeTask::getCreateTime)
                        .last("limit 8"));
        var recentResumes = recentResumeRows.stream()
                .map(task -> Map.of(
                        "traceId", task.getTraceId() == null ? "" : task.getTraceId(),
                        "fileName", task.getFileName() == null ? "" : task.getFileName(),
                        "status", task.getStatus() == null ? "" : task.getStatus(),
                        "score", task.getOverallScore() == null ? 0 : task.getOverallScore()))
                .toList();
        List<Map<String, Object>> sampleChunks = recentResumeRows.stream()
                .limit(5)
                .map(task -> {
                    Map<String, Object> chunk = new LinkedHashMap<>();
                    chunk.put("docId", task.getTraceId() == null ? "" : task.getTraceId());
                    chunk.put("source", task.getFileName() == null ? "" : task.getFileName());
                    chunk.put("docType", task.getFileName() != null && task.getFileName().toLowerCase().endsWith(".pdf") ? "resume_pdf" : "resume_text");
                    chunk.put("sectionPath", "result.summary");
                    chunk.put("contentPreview", task.getSummary() == null ? "" : task.getSummary());
                    chunk.put("metadata", Map.of(
                            "score", task.getOverallScore() == null ? 0 : task.getOverallScore(),
                            "recommendation", task.getRecommendation() == null ? "" : task.getRecommendation(),
                            "status", task.getStatus() == null ? "" : task.getStatus()));
                    return chunk;
                })
                .toList();

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("embeddingProvider", embeddingProperties.getProvider());
        response.put("embeddingOperational", embeddingAvailability.isOperational());
        response.put("jdCount", jdCount);
        response.put("resumeCount", taskCount);
        response.put("pdfCount", pdfCount);
        response.put("corpus", List.of(
                Map.of("name", "resume_pdf", "count", pdfCount, "description", "PDF 简历解析后的结构化文本与评估结果"),
                Map.of("name", "resume_text", "count", taskCount - pdfCount, "description", "文本/TXT/Markdown 简历"),
                Map.of("name", "jd_library", "count", jdCount, "description", "岗位 JD、技能要求、缺口与匹配标签")));
        response.put("ingestionPipeline", List.of(
                "PDFBox text extraction",
                "normalize whitespace and remove binary/null chars",
                "section-aware resume structure extraction",
                "parent document metadata preservation",
                "child chunks for lexical/vector retrieval",
                "hybrid retrieval + agentic rerank/usefulness"));
        response.put("chunkSchema", Map.of(
                "docId", "traceId or jdId",
                "docType", "resume_pdf | resume_text | jd",
                "sectionPath", "summary/skills/experience/projects/education/risk/report",
                "content", "retrievable text chunk",
                "metadata", List.of("candidateRole", "fileName", "page", "section", "score", "recommendation", "createdAt")));
        response.put("indexes", List.of(
                Map.of("name", "lexical_bm25_like", "type", "in-memory lexical scoring", "purpose", "exact terms / sparse resume / fast path"),
                Map.of("name", "milvus_embedding", "type", "vector", "provider", embeddingProperties.getProvider(), "purpose", "semantic recall"),
                Map.of("name", "milvus_kb_chunks", "type", "vector", "purpose", "knowledge-base hybrid retrieval"),
                Map.of("name", "llm_listwise_rerank", "type", "rerank", "purpose", "optional DeepSeek Top-20 listwise rerank")));
        response.put("retrievalPipeline", List.of(
                "route strategy by document length and query",
                "retrieve lexical candidates (BM25-like)",
                "retrieve embedding candidates when operational",
                "RRF fusion (k=60)",
                "optional DeepSeek listwise rerank when rerankerEnabled",
                "return selectedChunks with metadata to downstream agents"));
        response.put("evaluationSet", Map.of(
                "generatedPdfDataset", "testdata/resumes/metadata.json",
                "defaultSize", 300,
                "metrics", List.of("coverageRate", "hitCount", "topScore", "fallbackRate", "latencyMs", "usefulnessScore")));
        response.put("selfServiceKnowledgeBase", knowledgeBaseDocumentService.overview());
        response.put("agentMemory", agentMemoryService.overview());
        response.put("ragStrategies", List.of(
                "hybrid_bm25_embedding", "lexical_bm25_like", "embedding_only",
                "hybrid_bm25_embedding+llm_rerank"));
        response.put("recentJds", recentJds);
        response.put("recentResumes", recentResumes);
        response.put("sampleChunks", sampleChunks);
        return response;
    }

    @PostMapping("/knowledge-base/documents")
    public Map<String, Object> ingestKnowledgeDocument(@RequestBody KnowledgeDocumentRequest request) {
        return Map.of("document", knowledgeBaseDocumentService.ingestText(
                request.title(), request.content(), request.docType(), request.tags()));
    }

    @PostMapping("/knowledge-base/upload")
    public Map<String, Object> uploadKnowledgeDocument(@RequestParam("file") MultipartFile file,
                                                       @RequestParam(value = "title", required = false) String title,
                                                       @RequestParam(value = "docType", required = false) String docType,
                                                       @RequestParam(value = "tags", required = false) String tags) {
        return Map.of("document", knowledgeBaseDocumentService.ingestFile(file, title, docType, tags));
    }

    @PostMapping("/knowledge-base/search")
    public Map<String, Object> searchKnowledgeDocument(@RequestBody KnowledgeSearchRequest request) {
        int topK = request.topK() != null ? request.topK() : 5;
        boolean rerank = Boolean.TRUE.equals(request.rerank());
        KnowledgeBaseDocumentService.SearchResult result =
                knowledgeBaseDocumentService.searchDetailed(request.query(), topK, rerank);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("chunks", result.chunks());
        body.put("strategy", result.strategy());
        body.put("lexicalHits", result.lexicalHits());
        body.put("vectorHits", result.vectorHits());
        body.put("fusion", result.fusion());
        body.put("rerankApplied", result.rerankApplied());
        body.put("fallbackStage", result.fallbackStage());
        body.put("fallbackChain", result.fallbackChain());
        return body;
    }

    @PostMapping("/knowledge-base/reindex")
    public Map<String, Object> reindexKnowledgeBase() {
        return knowledgeBaseDocumentService.reindexAll();
    }

    @GetMapping("/knowledge-base/documents")
    public Map<String, Object> listKnowledgeDocuments() {
        return Map.of("documents", knowledgeBaseDocumentService.listDocuments());
    }

    @GetMapping("/knowledge-base/documents/{docId}")
    public Map<String, Object> getKnowledgeDocument(@PathVariable String docId) {
        return knowledgeBaseDocumentService.getDocument(docId);
    }

    @DeleteMapping("/knowledge-base/documents/{docId}")
    public Map<String, Object> deleteKnowledgeDocument(@PathVariable String docId) {
        boolean removed = knowledgeBaseDocumentService.deleteDocument(docId);
        return Map.of("removed", removed, "docId", docId);
    }

    @PostMapping("/memory/search")
    public Map<String, Object> searchAgentMemory(@RequestBody KnowledgeSearchRequest request) {
        int topK = request.topK() != null ? request.topK() : 5;
        return agentMemoryService.search(request.query(), topK);
    }

    private Object loadPresets() {
        try {
            var resource = new ClassPathResource("rag-presets.json");
            return objectMapper.readTree(resource.getInputStream()).get("presets");
        } catch (Exception e) {
            return List.of();
        }
    }

    public record PreviewRequest(String resumeText, String jdId, RagOptions options) {}

    public record CompareRequest(String resumeText, List<VariantRequest> variants) {}

    public record VariantRequest(String name, RagOptions options) {}

    public record KnowledgeDocumentRequest(String title, String content, String docType, String tags) {}

    public record KnowledgeSearchRequest(String query, Integer topK, Boolean rerank) {}
}
