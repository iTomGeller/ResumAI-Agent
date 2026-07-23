package com.resumai.agent.api.dto;

import com.fasterxml.jackson.annotation.JsonAlias;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * JD 匹配结果。
 * <p>
 * {@code matchScore} 为业务维度匹配分（0-1），与检索/RRF 分值分离。
 * {@code score} 仅为兼容旧客户端的别名，语义等同 {@code matchScore}。
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record JdMatchResult(
        String jdId,
        String title,
        String category,
        @JsonProperty("matchScore")
        @JsonAlias("score")
        double matchScore,
        List<String> matchReasons,
        List<String> gaps,
        List<String> interviewChecks,
        Double skillMatchScore,
        Double experienceMatchScore,
        Double projectMatchScore,
        Double riskPenalty,
        /** 融合后的召回解释分（hybrid 下通常等于 rrfScore） */
        Double retrievalScore,
        Double vectorScore,
        Double bm25Score,
        Double rrfScore,
        /** 文档级溯源（documentId≈jdId；JD 路通常无 chunk/offset） */
        RagProvenance provenance
) {
    public JdMatchResult(String jdId, String title, String category, double matchScore) {
        this(jdId, title, category, matchScore, List.of(), List.of(), List.of(),
                null, null, null, null, null, null, null, null, null);
    }

    public JdMatchResult(String jdId, String title, String category, double matchScore,
                         List<String> matchReasons, List<String> gaps, List<String> interviewChecks) {
        this(jdId, title, category, matchScore, matchReasons, gaps, interviewChecks,
                null, null, null, null, null, null, null, null, null);
    }

    public JdMatchResult(String jdId, String title, String category, double matchScore,
                         List<String> matchReasons, List<String> gaps, List<String> interviewChecks,
                         Double skillMatchScore, Double experienceMatchScore,
                         Double projectMatchScore, Double riskPenalty) {
        this(jdId, title, category, matchScore, matchReasons, gaps, interviewChecks,
                skillMatchScore, experienceMatchScore, projectMatchScore, riskPenalty,
                null, null, null, null, null);
    }

    /**
     * @deprecated 使用 {@link #matchScore()}；保留以兼容旧 JSON 字段名与调用方。
     */
    @Deprecated
    @JsonProperty("score")
    public double score() {
        return matchScore;
    }

    /**
     * 附加检索通道分数，保留业务 {@code matchScore} 不变。
     */
    public JdMatchResult withRetrieval(double rrfScore, Double vectorScore, Double bm25Score) {
        Double mergedVector = vectorScore != null ? vectorScore : this.vectorScore;
        Double mergedBm25 = bm25Score != null ? bm25Score : this.bm25Score;
        return new JdMatchResult(
                jdId,
                title,
                category,
                matchScore,
                matchReasons,
                gaps,
                interviewChecks,
                skillMatchScore,
                experienceMatchScore,
                projectMatchScore,
                riskPenalty,
                rrfScore,
                mergedVector,
                mergedBm25,
                rrfScore,
                provenance);
    }

    /**
     * 仅标记单通道召回分（向量或 BM25），业务分不变。
     */
    public JdMatchResult withChannelScores(Double vectorScore, Double bm25Score) {
        Double retrieval = vectorScore != null ? vectorScore : bm25Score;
        return new JdMatchResult(
                jdId,
                title,
                category,
                matchScore,
                matchReasons,
                gaps,
                interviewChecks,
                skillMatchScore,
                experienceMatchScore,
                projectMatchScore,
                riskPenalty,
                retrieval != null ? retrieval : retrievalScore,
                vectorScore != null ? vectorScore : this.vectorScore,
                bm25Score != null ? bm25Score : this.bm25Score,
                rrfScore,
                provenance);
    }

    public JdMatchResult withProvenance(RagProvenance provenance) {
        return new JdMatchResult(
                jdId,
                title,
                category,
                matchScore,
                matchReasons,
                gaps,
                interviewChecks,
                skillMatchScore,
                experienceMatchScore,
                projectMatchScore,
                riskPenalty,
                retrievalScore,
                vectorScore,
                bm25Score,
                rrfScore,
                provenance);
    }
}
