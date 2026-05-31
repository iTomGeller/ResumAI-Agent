package com.resumai.agent.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.config.EmbeddingAvailability;
import com.resumai.agent.config.EmbeddingProperties;
import com.resumai.agent.rag.RagOptions;
import com.resumai.agent.service.HybridRagService;
import com.resumai.agent.service.RagAdvisorService;
import com.resumai.agent.service.RagConfigService;
import java.util.List;
import java.util.Map;
import org.springframework.core.io.ClassPathResource;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/rag")
public class RagController {

    private final RagConfigService ragConfigService;
    private final HybridRagService hybridRagService;
    private final RagAdvisorService ragAdvisorService;
    private final EmbeddingAvailability embeddingAvailability;
    private final EmbeddingProperties embeddingProperties;
    private final ObjectMapper objectMapper;

    public RagController(RagConfigService ragConfigService,
                         HybridRagService hybridRagService,
                         RagAdvisorService ragAdvisorService,
                         EmbeddingAvailability embeddingAvailability,
                         EmbeddingProperties embeddingProperties,
                         ObjectMapper objectMapper) {
        this.ragConfigService = ragConfigService;
        this.hybridRagService = hybridRagService;
        this.ragAdvisorService = ragAdvisorService;
        this.embeddingAvailability = embeddingAvailability;
        this.embeddingProperties = embeddingProperties;
        this.objectMapper = objectMapper;
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
}
