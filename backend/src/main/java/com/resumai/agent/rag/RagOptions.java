package com.resumai.agent.rag;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

@JsonIgnoreProperties(ignoreUnknown = true)
public record RagOptions(
        String strategy,
        int topK,
        double scoreThreshold,
        double semanticWeight,
        double keywordWeight,
        int rrfK,
        boolean rerankerEnabled,
        String rerankerModel,
        int chunkSize,
        int chunkOverlap,
        String embeddingProvider,
        Generation generation,
        String presetName
) {
    public record Generation(
            double temperature,
            double topP,
            int maxTokens
    ) {
        public static Generation defaults() {
            return new Generation(0.4, 0.9, 1200);
        }
    }

    public static RagOptions defaults() {
        return new RagOptions(
                "hybrid",
                5,
                0.35,
                0.7,
                0.3,
                60,
                false,
                "none",
                400,
                80,
                "local",
                Generation.defaults(),
                "balanced"
        );
    }

    public RagOptions withStrategy(String newStrategy) {
        return new RagOptions(newStrategy, topK, scoreThreshold, semanticWeight, keywordWeight,
                rrfK, rerankerEnabled, rerankerModel, chunkSize, chunkOverlap,
                embeddingProvider, generation, presetName);
    }

    public RagOptions withTopK(int newTopK) {
        return new RagOptions(strategy, newTopK, scoreThreshold, semanticWeight, keywordWeight,
                rrfK, rerankerEnabled, rerankerModel, chunkSize, chunkOverlap,
                embeddingProvider, generation, presetName);
    }
}
