package com.resumai.agent.api;

import com.resumai.agent.api.dto.InternalJdSearchRequest;
import com.resumai.agent.api.dto.InternalProfileRequest;
import com.resumai.agent.api.dto.InternalResumeSearchRequest;
import com.resumai.agent.api.dto.InternalSkillExecuteRequest;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.ai.SkillProvider;
import com.resumai.agent.rag.RagOptions;
import com.resumai.agent.service.ExternalProfileService;
import com.resumai.agent.service.AgentMemoryService;
import com.resumai.agent.service.HybridRagService;
import com.resumai.agent.service.InternalWorkflowService;
import com.resumai.agent.service.JdRagService;
import com.resumai.agent.service.KnowledgeBaseDocumentService;
import com.resumai.agent.service.ResumeRagService;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.util.StringUtils;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/internal")
public class InternalWorkflowController {

    private final InternalWorkflowService internalWorkflowService;
    private final ResumeRagService resumeRagService;
    private final JdRagService jdRagService;
    private final HybridRagService hybridRagService;
    private final ExternalProfileService externalProfileService;
    private final SkillProvider skillProvider;
    private final KnowledgeBaseDocumentService knowledgeBaseDocumentService;
    private final AgentMemoryService agentMemoryService;

    public InternalWorkflowController(InternalWorkflowService internalWorkflowService,
                                      ResumeRagService resumeRagService,
                                      JdRagService jdRagService,
                                      HybridRagService hybridRagService,
                                      ExternalProfileService externalProfileService,
                                      SkillProvider skillProvider,
                                      KnowledgeBaseDocumentService knowledgeBaseDocumentService,
                                      AgentMemoryService agentMemoryService) {
        this.internalWorkflowService = internalWorkflowService;
        this.resumeRagService = resumeRagService;
        this.jdRagService = jdRagService;
        this.hybridRagService = hybridRagService;
        this.externalProfileService = externalProfileService;
        this.skillProvider = skillProvider;
        this.knowledgeBaseDocumentService = knowledgeBaseDocumentService;
        this.agentMemoryService = agentMemoryService;
    }

    private void authorize(String token) {
        if (!internalWorkflowService.authorize(token)) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "invalid internal token");
        }
    }

    @PostMapping("/tools/resume-search")
    public Map<String, Object> resumeSearch(@RequestHeader("X-Internal-Token") String token,
                                            @RequestBody InternalResumeSearchRequest request) {
        authorize(token);
        int topK = request.topK() != null ? request.topK() : 5;
        ResumeRagService.RagRetrieveResult result = resumeRagService.retrieveDetailed(
                request.query(), topK, request.resumeText(), request.jdRequirements(), request.strategy());
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("chunks", result.chunks());
        body.put("hitCount", result.hitCount());
        body.put("topScore", result.topScore());
        body.put("fallbackUsed", result.fallbackUsed());
        body.put("fallbackReason", result.fallbackReason());
        body.put("backend", result.backend());
        body.put("strategy", result.strategy());
        body.put("errorType", result.errorType());
        body.put("query", result.query());
        body.put("usedResumeTextFallback", result.usedResumeTextFallback());
        Map<String, Object> rerank = rerankChunks(request.query(), result.chunks());
        body.put("selectedChunks", rerank.get("selectedChunks"));
        body.put("usefulnessScore", rerank.get("usefulnessScore"));
        body.put("rerankStrategy", rerank.get("rerankStrategy"));
        body.put("rerankScores", rerank.get("rerankScores"));
        // Agentic RAG: explicit, inspectable pipeline + self-reflection on evidence sufficiency
        // (ReflectiveRAG pattern). Makes it clear this is hybrid + rerank + reflect, not naive embedding.
        body.put("ragPipeline", List.of(
                "hybrid_retrieve(dense=Milvus + lexical=bm25-like)",
                "rrf_merge",
                "rerank(overlap-density + length)",
                "reflect(evidence_sufficiency)"));
        body.put("evidenceSufficiency", rerank.get("evidenceSufficiency"));
        body.put("knowledgeHits", knowledgeBaseDocumentService.search(request.query(), Math.min(topK, 5)));
        return body;
    }

    private Map<String, Object> rerankChunks(String query, List<String> chunks) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (chunks == null || chunks.isEmpty()) {
            out.put("selectedChunks", List.of());
            out.put("usefulnessScore", 0.0);
            out.put("rerankStrategy", "hybrid_rrf_rerank_reflect");
            out.put("rerankScores", List.of());
            out.put("evidenceSufficiency", Map.of("sufficient", false, "reason", "no_candidates_retrieved", "action", "fallback_to_resume_text"));
            return out;
        }
        List<String> terms = java.util.Arrays.stream((query == null ? "" : query).split("[\\s,，、/|；;:：()（）]+"))
                .map(String::trim)
                .filter(s -> s.length() >= 2)
                .map(String::toLowerCase)
                .distinct()
                .toList();
        List<Map.Entry<String, Double>> scored = new java.util.ArrayList<>();
        for (String chunk : chunks) {
            String lower = chunk == null ? "" : chunk.toLowerCase();
            long matched = terms.stream().filter(lower::contains).count();
            double density = terms.isEmpty() ? 0.5 : (double) matched / Math.max(terms.size(), 1);
            double lengthSignal = Math.min((chunk == null ? 0 : chunk.length()) / 300.0, 1.0);
            double score = Math.min(1.0, 0.75 * density + 0.25 * lengthSignal);
            scored.add(Map.entry(chunk, score));
        }
        scored.sort((a, b) -> Double.compare(b.getValue(), a.getValue()));
        List<String> selected = scored.stream()
                .filter(e -> e.getValue() >= 0.25)
                .map(Map.Entry::getKey)
                .limit(3)
                .toList();
        if (selected.isEmpty()) {
            selected = scored.stream().map(Map.Entry::getKey).limit(1).toList();
        }
        List<Double> topScores = scored.stream().limit(3).map(e -> Math.round(e.getValue() * 1000.0) / 1000.0).toList();
        double usefulness = scored.stream().mapToDouble(Map.Entry::getValue).max().orElse(0.0);
        long usefulCount = scored.stream().filter(e -> e.getValue() >= 0.25).count();
        // Self-reflection: is the retrieved evidence sufficient, or should we flag a fallback?
        boolean sufficient = usefulness >= 0.4 && usefulCount >= 1;
        Map<String, Object> sufficiency = new LinkedHashMap<>();
        sufficiency.put("sufficient", sufficient);
        sufficiency.put("usefulCandidates", usefulCount);
        sufficiency.put("topUsefulness", Math.round(usefulness * 1000.0) / 1000.0);
        sufficiency.put("reason", sufficient
                ? "top candidate relevance above threshold"
                : "weak overlap; rely more on full resume text and flag low RAG usefulness");
        sufficiency.put("action", sufficient ? "inject_selected_chunks" : "downweight_rag_use_resume_text");
        out.put("selectedChunks", selected);
        out.put("usefulnessScore", usefulness);
        out.put("rerankStrategy", "hybrid_rrf_rerank_reflect");
        out.put("rerankScores", topScores);
        out.put("evidenceSufficiency", sufficiency);
        return out;
    }

    @PostMapping("/tools/jd-search")
    public Map<String, Object> jdSearch(@RequestHeader("X-Internal-Token") String token,
                                        @RequestBody InternalJdSearchRequest request) {
        authorize(token);
        int topK = request.topK() != null ? request.topK() : 3;
        // 与 AutoMatch 同源：hybrid-RRF（可选 rerank），不再走 vector-or-lexical 单通道。
        List<JdMatchResult> items = hybridRagService.retrieve(
                request.resumeText(), RagOptions.defaults().withTopK(topK));
        double topScore = items.stream().mapToDouble(JdMatchResult::score).max().orElse(0D);
        String effectiveJd = "";
        if (!items.isEmpty()) {
            effectiveJd = jdRagService.getJdDescription(items.get(0).jdId());
            if (!StringUtils.hasText(effectiveJd) && StringUtils.hasText(items.get(0).title())) {
                effectiveJd = items.get(0).title();
            }
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("items", items);
        body.put("effectiveJd", effectiveJd);
        body.put("hitCount", items.size());
        body.put("topScore", topScore);
        body.put("fallbackUsed", items.isEmpty());
        body.put("fallbackReason", items.isEmpty() ? "no_jd_match" : null);
        body.put("strategy", "hybrid");
        return body;
    }

    @PostMapping("/tools/external-profile")
    public Map<String, String> externalProfile(@RequestHeader("X-Internal-Token") String token,
                                               @RequestBody InternalProfileRequest request) {
        authorize(token);
        return Map.of("summary", externalProfileService.enrich(request.resumeText()));
    }

    @PostMapping("/tools/skills/list")
    public List<Map<String, String>> listSkills(@RequestHeader("X-Internal-Token") String token) {
        authorize(token);
        return skillProvider.listInstalled().stream()
                .map(skill -> Map.of(
                        "name", skill.name(),
                        "description", skill.description() != null ? skill.description() : ""))
                .toList();
    }

    @PostMapping("/tools/skills/execute")
    public Map<String, Object> executeSkill(@RequestHeader("X-Internal-Token") String token,
                                            @RequestBody InternalSkillExecuteRequest request) {
        authorize(token);
        Map<String, Object> result = new LinkedHashMap<>(skillProvider.executeStructured(request.skillName(), request.task()));
        result.put("task", request.task() != null ? request.task() : "");
        return result;
    }

    @PostMapping("/tools/memory/search")
    public Map<String, Object> memorySearch(@RequestHeader("X-Internal-Token") String token,
                                            @RequestBody MemorySearchRequest request) {
        authorize(token);
        int topK = request.topK() != null ? Math.min(Math.max(request.topK(), 1), 8) : 5;
        return agentMemoryService.search(request.query(), topK);
    }

    public record MemorySearchRequest(String query, Integer topK) {}
}
