package com.resumai.agent.domain.dag;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 统一 DAG 步骤定义：依赖关系、排序、HR 标签与占位节点骨架。
 */
public final class DagStepRegistry {

    private DagStepRegistry() {
    }

    public record StepDefinition(
            String stepKind,
            String nodeId,
            String laneId,
            List<String> dependsOn,
            String edgeLabel,
            String phase,
            int sortOrder,
            String businessLabel,
            String developerLabel,
            String viewType,
            boolean optionalUpload
    ) {
        public static StepDefinition of(String stepKind, List<String> dependsOn, String edgeLabel, String phase,
                                        int sortOrder, String businessLabel, String developerLabel, String viewType) {
            return new StepDefinition(stepKind, stepKind, null, dependsOn, edgeLabel, phase, sortOrder,
                    businessLabel, developerLabel, viewType, false);
        }

        public static StepDefinition uploadStep() {
            return new StepDefinition("upload_parse", "upload_parse", null, List.of("task_create"),
                    "文件已解析", "bootstrap", 1, "上传解析", "CoordinatorAgent / UploadHandler", "BOTH", true);
        }

        public static StepDefinition parallelLane(String lane, int sortOrder, String businessLabel, String developerLabel) {
            return new StepDefinition("skill_eval", "skill_eval:" + lane, lane,
                    List.of("jd_match"), "并行评估", "parallel", sortOrder,
                    businessLabel, developerLabel, "BOTH", false);
        }
    }

    private static final List<StepDefinition> PIPELINE = List.of(
            StepDefinition.of("task_create", List.of(), "任务已接收", "bootstrap", 0,
                    "创建评估任务", "CoordinatorAgent / TaskBootstrap", "BOTH"),
            StepDefinition.uploadStep(),
            StepDefinition.of("resume_parse", List.of("upload_parse"), "简历已解析", "parse", 2,
                    "解析简历基本信息", "DeterministicPreflight / parse_resume", "BOTH"),
            StepDefinition.of("graph_extraction", List.of("resume_parse"), "图谱已抽取", "parse", 3,
                    "抽取知识图谱", "DeterministicPreflight / GraphExtraction", "DEV"),
            StepDefinition.of("jd_match", List.of("graph_extraction"), "岗位已匹配", "match", 4,
                    "自动匹配最合适岗位", "DeterministicPreflight / JdRagMatch", "BOTH"),
            StepDefinition.parallelLane("tech", 5, "评估技术能力", "TechAgent / TechStackAuditSkill"),
            StepDefinition.parallelLane("project", 6, "评估项目经历", "ProjectAgent / ProjectDepthSkill"),
            StepDefinition.parallelLane("risk", 7, "识别风险信号", "RiskAgent / RiskDetectionSkill"),
            StepDefinition.of("external_enrichment",
                    List.of("skill_eval:tech", "skill_eval:project", "skill_eval:risk"),
                    "外部资料已检索", "evidence", 8,
                    "检索外部作品", "ProjectAgent / PublicEvidenceMCP", "DEV"),
            StepDefinition.of("rag_index",
                    List.of("external_enrichment"),
                    "向量已索引", "evidence", 9,
                    "建立向量索引", "HybridRagStrategy / MilvusIndex", "DEV"),
            StepDefinition.of("historical_match",
                    List.of("rag_index"),
                    "历史候选人已匹配", "evidence", 10,
                    "检索相似候选人", "MemoryRetrieval / MilvusSearch", "DEV"),
            StepDefinition.of("jd_requirements",
                    List.of("historical_match"),
                    "JD 需求已结构化", "evidence", 11,
                    "提取 JD 结构化要求", "DeterministicPreflight / RequirementExtraction", "DEV"),
            StepDefinition.of("rag_retrieve",
                    List.of("jd_requirements"),
                    "证据已融合", "evidence", 12,
                    "融合多源证据", "HybridRagStrategy / MilvusSearch + BM25", "BOTH"),
            StepDefinition.of("llm_complete", List.of("rag_retrieve"), "AI 已评估", "evaluate", 13,
                    "AI生成评估报告", "DeepSeekChatModel / ChatCompletion", "BOTH"),
            StepDefinition.of("quality_check", List.of("llm_complete"), "质量已校验", "quality", 14,
                    "报告结构校验", "ReportAgent / SchemaValidation", "DEV"),
            StepDefinition.of("report_generate", List.of("quality_check"), "报告已生成", "report", 15,
                    "生成评估报告", "ReportAgent / ReportAssembly", "BOTH")
    );

    private static final Map<String, StepDefinition> BY_NODE_ID = new LinkedHashMap<>();

    static {
        for (StepDefinition def : PIPELINE) {
            BY_NODE_ID.put(def.nodeId(), def);
        }
    }

    public static List<StepDefinition> pipeline() {
        return PIPELINE;
    }

    public static Optional<StepDefinition> findByStepKind(String stepKind, String laneId) {
        if (stepKind == null) {
            return Optional.empty();
        }
        if ("skill_eval".equals(stepKind) && laneId != null) {
            return Optional.ofNullable(BY_NODE_ID.get("skill_eval:" + laneId));
        }
        return PIPELINE.stream()
                .filter(d -> stepKind.equals(d.stepKind()) && d.laneId() == null)
                .findFirst();
    }

    public static Optional<StepDefinition> findByNodeId(String nodeId) {
        return Optional.ofNullable(BY_NODE_ID.get(nodeId));
    }

    public static List<String> dependsOnFor(String stepKind, String laneId) {
        return findByStepKind(stepKind, laneId).map(StepDefinition::dependsOn).orElse(List.of());
    }

    public static String resolveNodeId(String stepKind, String laneId) {
        if ("skill_eval".equals(stepKind) && laneId != null) {
            return "skill_eval:" + laneId;
        }
        return stepKind;
    }

    public static List<StepDefinition> skeleton(boolean includeUpload) {
        List<StepDefinition> result = new ArrayList<>();
        for (StepDefinition def : PIPELINE) {
            if (def.optionalUpload() && !includeUpload) {
                continue;
            }
            if ("resume_parse".equals(def.stepKind()) && !includeUpload) {
                result.add(StepDefinition.of("resume_parse", List.of("task_create"), def.edgeLabel(), def.phase(),
                        def.sortOrder(), def.businessLabel(), def.developerLabel(), def.viewType()));
            } else if ("graph_extraction".equals(def.stepKind()) && !includeUpload) {
                result.add(StepDefinition.of("graph_extraction", List.of("resume_parse"), def.edgeLabel(), def.phase(),
                        def.sortOrder(), def.businessLabel(), def.developerLabel(), def.viewType()));
            } else {
                result.add(def);
            }
        }
        return result;
    }
}
