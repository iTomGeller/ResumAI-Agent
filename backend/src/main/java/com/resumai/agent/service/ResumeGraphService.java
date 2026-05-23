package com.resumai.agent.service;

import com.resumai.agent.config.AgentMetrics;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.ai.DeepSeekClient;
import com.resumai.agent.api.dto.GraphResponse;
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

    public ResumeGraphService(Driver driver, DeepSeekClient deepSeekClient, ObjectMapper objectMapper, AgentMetrics agentMetrics) {
        this.driver = driver;
        this.deepSeekClient = deepSeekClient;
        this.objectMapper = objectMapper;
        this.agentMetrics = agentMetrics;
    }

    public boolean isNeo4jAvailable() {
        return driver != null;
    }

    /**
     * 用 LLM 从简历中提取实体并写入 Neo4j 图谱。
     */
    public void populateGraph(String traceId, String resumeText, String jobCategory) {
        long start = System.currentTimeMillis();
        try {
            String extractionPrompt = buildExtractionPrompt(resumeText, jobCategory);
            String json = deepSeekClient.evaluateResume(extractionPrompt, "ResumeGraphAgent", "graph_extraction");

            String cleaned = json.trim();
            if (cleaned.startsWith("```")) {
                cleaned = cleaned.replaceAll("^```[a-zA-Z]*\\n?", "").replaceAll("```$", "").trim();
            }

            Map<String, Object> entities = objectMapper.readValue(cleaned, new TypeReference<>() {});
            writeToNeo4j(traceId, entities, jobCategory);
            agentMetrics.recordToolCall("neo4j_populate", "ResumeGraphAgent", "SUCCESS",
                    System.currentTimeMillis() - start);
        } catch (Exception e) {
            log.warn("Neo4j populateGraph failed for traceId={}: {}", traceId, e.getMessage());
            agentMetrics.recordToolCallError("neo4j_populate", e.getClass().getSimpleName());
            agentMetrics.recordToolCall("neo4j_populate", "ResumeGraphAgent", "FAILED",
                    System.currentTimeMillis() - start);
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
                String nodeId = targetNode.get("id").asString();
                String label = targetNode.get("name").asString();
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
        return nodes.isEmpty() ? null : new GraphResponse(nodes, edges);
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
    private void writeToNeo4j(String traceId, Map<String, Object> entities, String jobCategory) {
        String candidateName = (String) entities.getOrDefault("candidate_name", "候选人");
        List<Map<String, Object>> skills = (List<Map<String, Object>>) entities.getOrDefault("skills", List.of());
        List<Map<String, Object>> projects = (List<Map<String, Object>>) entities.getOrDefault("projects", List.of());
        List<Map<String, Object>> risks = (List<Map<String, Object>>) entities.getOrDefault("risks", List.of());
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
                String id = "skill_" + name.hashCode();
                session.run(
                        "MERGE (s:Skill {id: $id}) SET s.name = $name, s.score = $score " +
                        "WITH s MATCH (c:Candidate {traceId: $traceId}) " +
                        "MERGE (c)-[:POSSESSES {confidence: $conf}]->(s)",
                        Values.parameters("id", id, "name", name, "score", score, "traceId", traceId, "conf", score / 100.0)
                );
            }

            for (Map<String, Object> project : projects) {
                String name = (String) project.get("name");
                int score = ((Number) project.getOrDefault("score", 80)).intValue();
                String id = "proj_" + name.hashCode();
                session.run(
                        "MERGE (p:Project {id: $id}) SET p.name = $name, p.score = $score " +
                        "WITH p MATCH (c:Candidate {traceId: $traceId}) " +
                        "MERGE (c)-[:WORKED_ON {confidence: $conf}]->(p)",
                        Values.parameters("id", id, "name", name, "score", score, "traceId", traceId, "conf", score / 100.0)
                );
            }

            for (Map<String, Object> risk : risks) {
                String name = (String) risk.get("name");
                int score = ((Number) risk.getOrDefault("score", 40)).intValue();
                String id = "risk_" + name.hashCode();
                session.run(
                        "MERGE (r:Risk {id: $id}) SET r.name = $name, r.score = $score " +
                        "WITH r MATCH (c:Candidate {traceId: $traceId}) " +
                        "MERGE (c)-[:HAS_RISK {confidence: $conf}]->(r)",
                        Values.parameters("id", id, "name", name, "score", score, "traceId", traceId, "conf", score / 100.0)
                );
            }

            session.run(
                    "MERGE (j:Job {name: $job}) " +
                    "WITH j MATCH (c:Candidate {traceId: $traceId}) " +
                    "MERGE (c)-[:TARGETS {confidence: 0.85}]->(j)",
                    Values.parameters("job", jobCategory, "traceId", traceId)
            );
        }

        agentMetrics.recordNeo4jNodesWritten("populateGraph", nodesWritten);
        agentMetrics.recordNeo4jRelationshipsWritten("populateGraph", relationshipsWritten);
    }
}
