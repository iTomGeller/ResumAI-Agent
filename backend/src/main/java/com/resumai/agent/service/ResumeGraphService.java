package com.resumai.agent.service;

import com.resumai.agent.config.AgentMetrics;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.ai.LlmCallResult;
import com.resumai.agent.api.dto.GraphResponse;
import com.resumai.agent.api.dto.JdMatchResult;
import com.resumai.agent.rag.RagOptions;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Session;
import org.neo4j.driver.Result;
import org.neo4j.driver.Record;
import org.neo4j.driver.Values;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Neo4j 图谱服务 -- 将简历结构化实体写入图数据库并提供子图查询。
 */
@Service
public class ResumeGraphService {

    private static final Logger log = LoggerFactory.getLogger(ResumeGraphService.class);

    private final Driver driver;
    private final DeepSeekClient deepSeekClient;
    private final ObjectMapper objectMapper;
    private final AgentMetrics agentMetrics;
    private final JdRagService jdRagService;

    public ResumeGraphService(Driver driver, DeepSeekClient deepSeekClient, ObjectMapper objectMapper,
                              AgentMetrics agentMetrics, JdRagService jdRagService) {
        this.driver = driver;
        this.deepSeekClient = deepSeekClient;
        this.objectMapper = objectMapper;
        this.agentMetrics = agentMetrics;
        this.jdRagService = jdRagService;
    }

    public boolean isNeo4jAvailable() {
        return driver != null;
    }

    public record GraphPopulateResult(String llmInvocationId, long durationMs, boolean success) {}

    /**
     * 用 LLM 从简历中提取实体并写入 Neo4j 图谱。
     */
    public GraphPopulateResult populateGraph(String traceId, String resumeText, String jobCategory) {
        return populateGraph(traceId, resumeText, jobCategory, jobCategory, "job-" + jobCategory);
    }

    public GraphPopulateResult populateGraph(String traceId, String resumeText, String jobCategory, String jobTitle, String jobId) {
        long start = System.currentTimeMillis();
        try {
            String extractionPrompt = buildExtractionPrompt(resumeText, jobCategory);
            LlmCallResult llmResult = deepSeekClient.evaluateResume(extractionPrompt, "ResumeGraphAgent", "graph_extraction", traceId, null);
            String json = llmResult.text();

            String cleaned = json.trim();
            if (cleaned.startsWith("```")) {
                cleaned = cleaned.replaceAll("^```[a-zA-Z]*\\n?", "").replaceAll("```$", "").trim();
            }

            Map<String, Object> entities = objectMapper.readValue(cleaned, new TypeReference<>() {});
            deleteSubgraph(traceId);
            writeToNeo4j(traceId, entities, jobCategory, jobTitle, jobId);
            long durationMs = System.currentTimeMillis() - start;
            agentMetrics.recordToolCall("neo4j_populate", "ResumeGraphAgent", "SUCCESS", durationMs);
            return new GraphPopulateResult(llmResult.llmInvocationId(), durationMs, true);
        } catch (Exception e) {
            log.warn("Neo4j populateGraph failed for traceId={}: {}", traceId, e.getMessage());
            agentMetrics.recordToolCallError("neo4j_populate", e.getClass().getSimpleName());
            long durationMs = System.currentTimeMillis() - start;
            agentMetrics.recordToolCall("neo4j_populate", "ResumeGraphAgent", "FAILED", durationMs);
            return new GraphPopulateResult(null, durationMs, false);
        }
    }

    /**
     * 查询 traceId 对应的子图，返回 GraphResponse。
     */
    public GraphResponse querySubgraph(String traceId) {
        long start = System.currentTimeMillis();
        List<GraphResponse.GraphNode> nodes = new ArrayList<>();
        List<GraphResponse.GraphEdge> edges = new ArrayList<>();

        try (Session session = driver.session()) {
            Result result = session.run(
                    "MATCH (c:Candidate {traceId: $traceId})-[r]->(n) " +
                    "RETURN c, type(r) AS rel, r.confidence AS conf, n, labels(n) AS nlabels",
                    Values.parameters("traceId", traceId)
            );

            boolean candidateAdded = false;
            while (result.hasNext()) {
                Record rec = result.next();
                if (!candidateAdded) {
                    String candidateName = rec.get("c").asNode().get("name").asString();
                    nodes.add(new GraphResponse.GraphNode("candidate", candidateName, "candidate", 86));
                    candidateAdded = true;
                }
                org.neo4j.driver.types.Node targetNode = rec.get("n").asNode();
                String nodeId = null;
                if (targetNode.containsKey("id") && !targetNode.get("id").isNull()) {
                    nodeId = targetNode.get("id").asString();
                }
                if (!StringUtils.hasText(nodeId) || "null".equalsIgnoreCase(nodeId)) {
                    continue;
                }
                String label = targetNode.containsKey("label") && !targetNode.get("label").isNull()
                        ? targetNode.get("label").asString()
                        : targetNode.get("name").asString();
                List<String> nlabels = rec.get("nlabels").asList(v -> v.asString());
                String type = nlabels.isEmpty() ? "skill" : nlabels.get(0).toLowerCase();
                int score = targetNode.containsKey("score") ? targetNode.get("score").asInt() : 80;
                nodes.add(new GraphResponse.GraphNode(nodeId, label, type, score));

                String relType = rec.get("rel").asString();
                double conf = rec.get("conf").isNull() ? 0.8 : rec.get("conf").asDouble();
                edges.add(new GraphResponse.GraphEdge("candidate", nodeId, relType, conf));
            }
            agentMetrics.recordToolCall("neo4j_query", "ResumeGraphAgent", "SUCCESS",
                    System.currentTimeMillis() - start);
        } catch (Exception e) {
            log.warn("Neo4j querySubgraph failed for traceId={}: {}", traceId, e.getMessage());
            agentMetrics.recordToolCallError("neo4j_query", e.getClass().getSimpleName());
            agentMetrics.recordToolCall("neo4j_query", "ResumeGraphAgent", "FAILED",
                    System.currentTimeMillis() - start);
        }

        agentMetrics.recordNeo4jNodesWritten("querySubgraph", nodes.size());
        return nodes.isEmpty() ? null : validateGraph(new GraphResponse(nodes, edges, "NEO4J"));
    }

    /**
     * 删除 traceId 对应子图，避免重复评估时跨候选人污染。
     */
    public void deleteSubgraph(String traceId) {
        if (!StringUtils.hasText(traceId) || driver == null) {
            return;
        }
        try (Session session = driver.session()) {
            session.run(
                    "MATCH (c:Candidate {traceId: $traceId})-[r]->(n) DELETE r",
                    Values.parameters("traceId", traceId));
            session.run(
                    "MATCH (c:Candidate {traceId: $traceId}) DELETE c",
                    Values.parameters("traceId", traceId));
        } catch (Exception e) {
            log.warn("Neo4j deleteSubgraph failed for traceId={}: {}", traceId, e.getMessage());
        }
    }

    private GraphResponse validateGraph(GraphResponse graph) {
        List<GraphResponse.GraphNode> validNodes = graph.nodes().stream()
                .filter(n -> StringUtils.hasText(n.id()) && !"null".equalsIgnoreCase(n.id()))
                .toList();
        java.util.Set<String> nodeIds = validNodes.stream().map(GraphResponse.GraphNode::id).collect(java.util.stream.Collectors.toSet());
        List<GraphResponse.GraphEdge> validEdges = graph.edges().stream()
                .filter(e -> nodeIds.contains(e.from()) && nodeIds.contains(e.to()))
                .toList();
        return new GraphResponse(validNodes, validEdges, graph.source());
    }

    private String buildExtractionPrompt(String resumeText, String jobCategory) {
        return "请从以下简历中提取结构化实体，目标岗位为「" + jobCategory + "」。\n"
                + "返回严格 JSON（不要加任何说明），格式：\n"
                + "{\n"
                + "  \"skills\": [{\"name\": \"xxx\", \"score\": 85}],\n"
                + "  \"projects\": [{\"name\": \"xxx\", \"score\": 80}],\n"
                + "  \"risks\": [{\"name\": \"xxx\", \"score\": 40}],\n"
                + "  \"candidate_name\": \"姓名\"\n"
                + "}\n\n"
                + "简历内容：\n" + (resumeText.length() > 3000 ? resumeText.substring(0, 3000) : resumeText);
    }

    @SuppressWarnings("unchecked")
    private void writeToNeo4j(String traceId, Map<String, Object> entities, String jobCategory, String jobTitle, String jobId) {
        String candidateName = (String) entities.getOrDefault("candidate_name", "候选人");
        List<Map<String, Object>> skills = (List<Map<String, Object>>) entities.getOrDefault("skills", List.of());
        List<Map<String, Object>> projects = (List<Map<String, Object>>) entities.getOrDefault("projects", List.of());
        List<Map<String, Object>> risks = (List<Map<String, Object>>) entities.getOrDefault("risks", List.of());
        String stableJobId = StringUtils.hasText(jobId) ? jobId : "job-" + jobCategory;
        String stableJobLabel = StringUtils.hasText(jobTitle) ? jobTitle : jobCategory;
        int nodesWritten = 1 + skills.size() + projects.size() + risks.size() + 1;
        int relationshipsWritten = skills.size() + projects.size() + risks.size() + 1;

        try (Session session = driver.session()) {
            session.run(
                    "MERGE (c:Candidate {traceId: $traceId}) SET c.name = $name",
                    Values.parameters("traceId", traceId, "name", candidateName)
            );

            for (Map<String, Object> skill : skills) {
                String name = (String) skill.get("name");
                int score = ((Number) skill.getOrDefault("score", 80)).intValue();
                String id = traceId + "_skill_" + name.hashCode();
                session.run(
                        "MERGE (s:Skill {id: $id}) SET s.name = $name, s.score = $score, s.traceId = $traceId " +
                        "WITH s MATCH (c:Candidate {traceId: $traceId}) " +
                        "MERGE (c)-[:POSSESSES {confidence: $conf}]->(s)",
                        Values.parameters("id", id, "name", name, "score", score, "traceId", traceId, "conf", score / 100.0)
                );
            }

            for (Map<String, Object> project : projects) {
                String name = (String) project.get("name");
                int score = ((Number) project.getOrDefault("score", 80)).intValue();
                String id = traceId + "_proj_" + name.hashCode();
                session.run(
                        "MERGE (p:Project {id: $id}) SET p.name = $name, p.score = $score, p.traceId = $traceId " +
                        "WITH p MATCH (c:Candidate {traceId: $traceId}) " +
                        "MERGE (c)-[:WORKED_ON {confidence: $conf}]->(p)",
                        Values.parameters("id", id, "name", name, "score", score, "traceId", traceId, "conf", score / 100.0)
                );
            }

            for (Map<String, Object> risk : risks) {
                String name = (String) risk.get("name");
                int score = ((Number) risk.getOrDefault("score", 40)).intValue();
                String id = traceId + "_risk_" + name.hashCode();
                session.run(
                        "MERGE (r:Risk {id: $id}) SET r.name = $name, r.score = $score, r.traceId = $traceId " +
                        "WITH r MATCH (c:Candidate {traceId: $traceId}) " +
                        "MERGE (c)-[:HAS_RISK {confidence: $conf}]->(r)",
                        Values.parameters("id", id, "name", name, "score", score, "traceId", traceId, "conf", score / 100.0)
                );
            }

            session.run(
                    "MERGE (j:Job {id: $jobId}) SET j.name = $jobName, j.label = $jobLabel, j.traceId = $traceId " +
                    "WITH j MATCH (c:Candidate {traceId: $traceId}) " +
                    "MERGE (c)-[:TARGETS {confidence: 0.85}]->(j)",
                    Values.parameters("jobId", stableJobId, "jobName", stableJobLabel, "jobLabel", stableJobLabel, "traceId", traceId)
            );
        }

        agentMetrics.recordNeo4jNodesWritten("populateGraph", nodesWritten);
        agentMetrics.recordNeo4jRelationshipsWritten("populateGraph", relationshipsWritten);
    }

    /**
     * GraphRAG：基于候选人技能/项目节点与 JD 技能重合度进行岗位匹配。
     */
    public List<JdMatchResult> matchViaGraph(String resumeText, RagOptions opts) {
        long start = System.currentTimeMillis();
        RagOptions effective = opts != null ? opts : RagOptions.defaults();
        List<JdMatchResult> lexical = jdRagService.matchTopJdsViaLexical(resumeText, Math.max(effective.topK() * 2, 10));
        if (!isNeo4jAvailable() || lexical.isEmpty()) {
            return lexical.stream().limit(effective.topK()).toList();
        }
        try (Session session = driver.session()) {
            Result skillResult = session.run(
                    "MATCH (s:Skill) RETURN s.name AS name LIMIT 200");
            List<String> graphSkills = new ArrayList<>();
            while (skillResult.hasNext()) {
                graphSkills.add(skillResult.next().get("name").asString().toLowerCase());
            }
            String resumeLower = resumeText.toLowerCase();
            List<JdMatchResult> boosted = new ArrayList<>();
            for (JdMatchResult match : lexical) {
                double boost = 0.0;
                for (String skill : graphSkills) {
                    if (resumeLower.contains(skill)) {
                        boost += 0.02;
                    }
                }
                double newScore = Math.min(1.0, match.score() + boost);
                boosted.add(new JdMatchResult(
                        match.jdId(), match.title(), match.category(), newScore,
                        match.matchReasons(), match.gaps(), match.interviewChecks(),
                        match.skillMatchScore(), match.experienceMatchScore(),
                        match.projectMatchScore(), match.riskPenalty()));
            }
            boosted.sort((a, b) -> Double.compare(b.score(), a.score()));
            agentMetrics.recordToolCall("neo4j_graph_match", "ResumeGraphService", "SUCCESS",
                    System.currentTimeMillis() - start);
            return boosted.stream().limit(effective.topK()).toList();
        } catch (Exception e) {
            log.warn("GraphRAG match failed: {}", e.getMessage());
            agentMetrics.recordToolCall("neo4j_graph_match", "ResumeGraphService", "WARNING",
                    System.currentTimeMillis() - start);
            return lexical.stream().limit(effective.topK()).toList();
        }
    }
}
