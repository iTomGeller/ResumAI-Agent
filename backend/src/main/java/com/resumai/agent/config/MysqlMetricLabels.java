package com.resumai.agent.config;

import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * MySQL 观测指标的中文业务标签映射，避免 Grafana 看板满屏英文。
 */
public final class MysqlMetricLabels {

    private static final Map<String, String> MAPPER_CN = Map.ofEntries(
            Map.entry("JdLibraryMapper", "岗位库"),
            Map.entry("ResumeTaskMapper", "评估任务"),
            Map.entry("AgentExecutionTraceMapper", "执行链路 Trace"),
            Map.entry("LlmInvocationMapper", "大模型调用记录"),
            Map.entry("HumanFeedbackLogMapper", "HR 反馈"),
            Map.entry("RagasEvalMetricsMapper", "RAGAS 质量评估"),
            Map.entry("SystemOrchestrationRuleMapper", "编排规则"),
            Map.entry("DynamicSkillPromptMapper", "动态 Skill 提示词"),
            Map.entry("MetaEvolutionHistoryMapper", "元进化历史"));

    private static final Map<String, String> METHOD_CN = Map.ofEntries(
            Map.entry("selectList", "查询列表"),
            Map.entry("selectOne", "查询单条"),
            Map.entry("selectById", "按 ID 查询"),
            Map.entry("selectCount", "计数查询"),
            Map.entry("selectMaps", "Map 查询"),
            Map.entry("selectObjs", "对象查询"),
            Map.entry("selectPage", "分页查询"),
            Map.entry("insert", "写入"),
            Map.entry("update", "更新"),
            Map.entry("updateById", "按 ID 更新"),
            Map.entry("delete", "删除"),
            Map.entry("deleteById", "按 ID 删除"));

    private static final Map<String, String> TABLE_CN = Map.ofEntries(
            Map.entry("jd_library", "岗位库"),
            Map.entry("resume_task", "评估任务"),
            Map.entry("agent_execution_trace", "执行链路"),
            Map.entry("llm_invocation", "大模型调用记录"),
            Map.entry("human_feedback_log", "HR 反馈"),
            Map.entry("ragas_eval_metrics", "RAGAS 评估"),
            Map.entry("system_orchestration_rule", "编排规则"),
            Map.entry("dynamic_skill_prompt", "动态 Skill 提示词"),
            Map.entry("meta_evolution_history", "元进化历史"));

    /** 表 -> 前端业务大类（JD / 候选人简历 / 执行链路等）。 */
    private static final Map<String, String> TABLE_BUSINESS_CATEGORY = Map.ofEntries(
            Map.entry("jd_library", "JD"),
            Map.entry("resume_task", "候选人简历"),
            Map.entry("agent_execution_trace", "执行链路"),
            Map.entry("llm_invocation", "AI 调用"),
            Map.entry("ragas_eval_metrics", "质量评估"),
            Map.entry("human_feedback_log", "反馈与配置"),
            Map.entry("system_orchestration_rule", "反馈与配置"),
            Map.entry("dynamic_skill_prompt", "反馈与配置"),
            Map.entry("meta_evolution_history", "其他"));

    /** Mapper -> 前端业务大类。 */
    private static final Map<String, String> MAPPER_BUSINESS_CATEGORY = Map.ofEntries(
            Map.entry("JdLibraryMapper", "JD"),
            Map.entry("ResumeTaskMapper", "候选人简历"),
            Map.entry("AgentExecutionTraceMapper", "执行链路"),
            Map.entry("LlmInvocationMapper", "AI 调用"),
            Map.entry("RagasEvalMetricsMapper", "质量评估"),
            Map.entry("HumanFeedbackLogMapper", "反馈与配置"),
            Map.entry("SystemOrchestrationRuleMapper", "反馈与配置"),
            Map.entry("DynamicSkillPromptMapper", "反馈与配置"),
            Map.entry("MetaEvolutionHistoryMapper", "其他"));

    private static final Map<String, String> SQL_TYPE_CN = Map.of(
            "SELECT", "查询",
            "INSERT", "写入",
            "UPDATE", "更新",
            "DELETE", "删除");

    private static final Pattern TABLE_PATTERN = Pattern.compile(
            "(?i)(?:FROM|UPDATE|INTO)\\s+[`']?([a-z][a-z0-9_]*)[`']?");

    private MysqlMetricLabels() {
    }

    public static String mapperCn(String mapper) {
        if (mapper == null || mapper.isBlank()) {
            return "未知模块";
        }
        return MAPPER_CN.getOrDefault(mapper, mapper);
    }

    public static String methodCn(String method) {
        if (method == null || method.isBlank()) {
            return "未知操作";
        }
        return METHOD_CN.getOrDefault(method, simplifyMethod(method));
    }

    public static String tableCn(String table) {
        if (table == null || table.isBlank()) {
            return "未知表";
        }
        return TABLE_CN.getOrDefault(table.toLowerCase(Locale.ROOT), table);
    }

    public static String sqlTypeCn(String sqlType) {
        if (sqlType == null || sqlType.isBlank()) {
            return "未知";
        }
        return SQL_TYPE_CN.getOrDefault(sqlType.toUpperCase(Locale.ROOT), sqlType);
    }

    public static String statusCn(boolean success) {
        return success ? "成功" : "失败";
    }

    /**
     * 将表/Mapper 归到前端业务大类，优先按表名映射。
     */
    public static String businessCategoryCn(String table, String mapper) {
        if (table != null && !table.isBlank()) {
            String byTable = TABLE_BUSINESS_CATEGORY.get(table.toLowerCase(Locale.ROOT));
            if (byTable != null) {
                return byTable;
            }
        }
        if (mapper != null && !mapper.isBlank()) {
            String byMapper = MAPPER_BUSINESS_CATEGORY.get(mapper);
            if (byMapper != null) {
                return byMapper;
            }
        }
        return "其他";
    }

    public static String extractMapper(String mappedStatementId) {
        if (mappedStatementId == null || mappedStatementId.isBlank()) {
            return "UnknownMapper";
        }
        int methodDot = mappedStatementId.lastIndexOf('.');
        if (methodDot <= 0) {
            return mappedStatementId;
        }
        String className = mappedStatementId.substring(0, methodDot);
        int mapperDot = className.lastIndexOf('.');
        return mapperDot >= 0 ? className.substring(mapperDot + 1) : className;
    }

    public static String extractMethod(String mappedStatementId) {
        if (mappedStatementId == null || mappedStatementId.isBlank()) {
            return "unknown";
        }
        int methodDot = mappedStatementId.lastIndexOf('.');
        return methodDot >= 0 ? mappedStatementId.substring(methodDot + 1) : "unknown";
    }

    public static String extractSqlType(String sql) {
        if (sql == null) {
            return "UNKNOWN";
        }
        String normalized = sql.strip();
        int space = normalized.indexOf(' ');
        String keyword = space > 0 ? normalized.substring(0, space) : normalized;
        return keyword.toUpperCase(Locale.ROOT);
    }

    public static String extractTable(String sql) {
        if (sql == null || sql.isBlank()) {
            return "unknown";
        }
        Matcher matcher = TABLE_PATTERN.matcher(sql);
        if (matcher.find()) {
            return matcher.group(1).toLowerCase(Locale.ROOT);
        }
        return "unknown";
    }

    private static String simplifyMethod(String method) {
        if (method.startsWith("select")) {
            return "查询";
        }
        if (method.startsWith("insert")) {
            return "写入";
        }
        if (method.startsWith("update")) {
            return "更新";
        }
        if (method.startsWith("delete")) {
            return "删除";
        }
        return method;
    }
}
