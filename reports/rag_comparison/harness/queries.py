"""
检索查询集（28 条，中英双语）。

每条查询：
  id           唯一标识
  cluster      目标岗位簇（仅用于文档说明，不参与检索）
  zh / en      中文 / 英文查询文本（检索时拼接两者，适配中英混合语料）
  target_skills  ground-truth 判定用的技能标签（英文，取自 manifest 词表）
  min_overlap    技能重叠阈值（默认 2）

注意：target_skills 只用于离线生成 ground truth，**检索器只能看到 zh/en 文本**，
不得使用 target_skills，否则构成数据泄漏。
"""
import json
import config

QUERIES = [
    # ---------------- 后端 ----------------
    {"id": "q01", "cluster": "资深后端", "zh": "高并发后端 Kafka 微服务 可观测性 SLA 治理",
     "en": "high concurrency backend Kafka microservices observability SLA",
     "target_skills": ["Kafka", "High Concurrency", "Microservices", "Observability", "Spring Boot", "JVM"]},
    {"id": "q02", "cluster": "后端(广义)", "zh": "Java Spring Boot MySQL Redis 后端开发工程师",
     "en": "Java Spring Boot MySQL Redis backend engineer",
     "target_skills": ["Java", "Spring Boot", "MySQL", "Redis"]},
    {"id": "q03", "cluster": "资深后端", "zh": "JVM 调优 SQL 优化 慢查询 性能排查",
     "en": "JVM tuning SQL optimization slow query performance troubleshooting",
     "target_skills": ["JVM", "SQL Optimization", "MySQL", "Observability"]},
    {"id": "q04", "cluster": "应届后端", "zh": "应届毕业生 Java 后端 数据结构 计算机网络 Git",
     "en": "new graduate Java backend data structures computer networks Git",
     "target_skills": ["Data Structures", "Computer Networks", "Git", "Java"]},
    {"id": "q24", "cluster": "资深后端", "zh": "分布式系统 微服务架构 服务治理 高可用",
     "en": "distributed systems microservices architecture service governance high availability",
     "target_skills": ["Microservices", "High Concurrency", "Spring Boot", "Observability"]},
    {"id": "q27", "cluster": "资深后端", "zh": "支付交易系统 结算 高并发 后端 Java",
     "en": "payment transaction settlement system high concurrency backend Java",
     "target_skills": ["High Concurrency", "Microservices", "Kafka", "JVM", "Java"]},

    # ---------------- AI Agent / 大模型 ----------------
    {"id": "q05", "cluster": "AI Agent", "zh": "RAG 向量检索 Agent 工程 LangGraph 工具编排",
     "en": "RAG vector retrieval agent engineering LangGraph tool orchestration",
     "target_skills": ["RAG", "Agent", "LangGraph", "MCP", "Tool Orchestration", "Spring AI"]},
    {"id": "q08", "cluster": "AI Agent", "zh": "Spring AI 工具编排 MCP 智能体后端",
     "en": "Spring AI tool orchestration MCP agent backend",
     "target_skills": ["Spring AI", "Tool Orchestration", "MCP", "Agent", "LangGraph"]},
    {"id": "q06", "cluster": "大模型应用", "zh": "大模型 RAG 评测 重排 向量检索 知识库问答",
     "en": "LLM RAG evaluation rerank vector search knowledge base QA",
     "target_skills": ["RAG", "Rerank", "Evaluation", "Vector Search", "Embedding", "Knowledge Base"]},
    {"id": "q07", "cluster": "大模型应用", "zh": "Prompt 工程 Embedding 嵌入 语义召回 向量",
     "en": "prompt engineering embedding semantic retrieval vector",
     "target_skills": ["Prompt Engineering", "Embedding", "Vector Search", "RAG"]},
    {"id": "q26", "cluster": "大模型应用", "zh": "大模型应用开发 LangChain 知识库 RAG 问答系统",
     "en": "LLM application development LangChain knowledge base RAG QA system",
     "target_skills": ["LangChain", "Knowledge Base", "RAG", "Prompt Engineering"]},
    {"id": "q23", "cluster": "AI 跨簇", "zh": "向量数据库 语义搜索 相似度召回 Milvus 嵌入",
     "en": "vector database semantic search similarity retrieval Milvus embedding",
     "target_skills": ["Milvus", "Vector Search", "Embedding", "RAG"]},

    # ---------------- 前端 ----------------
    {"id": "q09", "cluster": "资深前端", "zh": "前端性能优化 微前端 工程化 React 构建",
     "en": "frontend performance optimization micro-frontend engineering React build",
     "target_skills": ["Performance", "Micro-frontend", "Engineering", "React", "Webpack", "Node.js"]},
    {"id": "q25", "cluster": "资深前端", "zh": "Node.js 前端工程化 Webpack 构建 微前端",
     "en": "Node.js frontend engineering Webpack build tooling micro-frontend",
     "target_skills": ["Node.js", "Webpack", "Engineering", "Micro-frontend"]},
    {"id": "q10", "cluster": "初级前端", "zh": "Vue3 TypeScript Vite 组件化 Pinia 前端",
     "en": "Vue3 TypeScript Vite componentization Pinia frontend",
     "target_skills": ["Vue3", "TypeScript", "Vite", "Pinia", "Componentization", "ECharts"]},
    {"id": "q11", "cluster": "初级前端", "zh": "数据可视化 ECharts 图表 前端展示",
     "en": "data visualization ECharts charts dashboard frontend",
     "target_skills": ["ECharts", "Visualization", "CSS"]},
    {"id": "q28", "cluster": "前端跨簇", "zh": "React TypeScript 前端开发 单页应用",
     "en": "React TypeScript frontend development single page application",
     "target_skills": ["React", "TypeScript", "Vue3", "Performance"]},

    # ---------------- 数据平台 ----------------
    {"id": "q12", "cluster": "数据平台", "zh": "数据平台 Flink Spark 数仓 ETL Hive 数据治理",
     "en": "data platform Flink Spark data warehouse ETL Hive governance",
     "target_skills": ["Flink", "Spark", "Data Warehouse", "ETL", "Hive", "Data Governance"]},
    {"id": "q22", "cluster": "数据平台", "zh": "实时数仓 流式计算 Flink Spark 大数据",
     "en": "real-time data warehouse stream processing Flink Spark big data",
     "target_skills": ["Flink", "Spark", "Data Warehouse", "ETL"]},

    # ---------------- 运维 / SRE ----------------
    {"id": "q13", "cluster": "运维/SRE", "zh": "Kubernetes DevOps SRE 监控 CI/CD 容器化",
     "en": "Kubernetes DevOps SRE monitoring CI/CD Docker containerization",
     "target_skills": ["Kubernetes", "Docker", "Helm", "SRE", "CI/CD"]},
    {"id": "q21", "cluster": "运维/SRE", "zh": "可观测性 监控告警 Prometheus Grafana 指标",
     "en": "observability monitoring alerting Prometheus Grafana metrics",
     "target_skills": ["Observability", "Prometheus", "Grafana"]},

    # ---------------- 算法 / ML ----------------
    {"id": "q14", "cluster": "算法", "zh": "推荐算法 机器学习 PyTorch 特征工程 模型",
     "en": "recommendation algorithm machine learning PyTorch feature engineering model",
     "target_skills": ["Recommendation", "Machine Learning", "PyTorch", "Feature Engineering", "Model Serving", "MLflow"]},
    {"id": "q15", "cluster": "算法", "zh": "模型部署 上线 MLflow 模型服务 A/B 实验",
     "en": "model serving deployment MLflow model serving A/B testing experiment",
     "target_skills": ["Model Serving", "MLflow", "A/B Testing", "Machine Learning"]},

    # ---------------- 测试 / 安全 / 移动 / 产品 ----------------
    {"id": "q16", "cluster": "测试", "zh": "自动化测试 Selenium 接口测试 性能测试 Pytest",
     "en": "automation testing Selenium API testing performance testing pytest",
     "target_skills": ["Test Development", "Selenium", "API Testing", "Performance Testing", "Pytest", "Quality Assurance"]},
    {"id": "q17", "cluster": "安全", "zh": "安全开发 风控 漏洞 合规 加密 OAuth2 审计",
     "en": "secure development risk control vulnerability compliance encryption OAuth2 audit",
     "target_skills": ["Secure Development", "Risk Control", "Vulnerability", "Compliance", "Encryption", "OAuth2", "Audit"]},
    {"id": "q18", "cluster": "移动端", "zh": "Android Kotlin 移动端 崩溃治理 Jetpack 支付SDK",
     "en": "Android Kotlin mobile crash governance Jetpack payment SDK",
     "target_skills": ["Android", "Kotlin", "Crash Governance", "Jetpack", "Payment SDK"]},
    {"id": "q19", "cluster": "产品经理", "zh": "AI 产品经理 需求分析 PRD 用户研究 商业化",
     "en": "AI product manager requirement analysis PRD user research monetization",
     "target_skills": ["Requirement Analysis", "PRD", "User Research", "Monetization", "LLM Application", "Project Management"]},
    {"id": "q20", "cluster": "产品经理", "zh": "A/B 测试 数据分析 用户研究 增长",
     "en": "A/B testing data analysis user research growth",
     "target_skills": ["A/B Testing", "Data Analysis", "User Research"]},
]


def query_text(q):
    """检索器看到的查询文本：中文 + 英文拼接。"""
    return (q["zh"] + " " + q["en"]).strip()


def write_queries():
    for q in QUERIES:
        q.setdefault("min_overlap", config.DEFAULT_MIN_OVERLAP)
    payload = {
        "description": "中英双语检索查询集；检索器仅可见 zh/en，target_skills 仅用于离线生成 ground truth。",
        "count": len(QUERIES),
        "min_overlap_default": config.DEFAULT_MIN_OVERLAP,
        "queries": QUERIES,
    }
    with open(config.QUERIES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return QUERIES


if __name__ == "__main__":
    write_queries()
    print("wrote %d queries -> %s" % (len(QUERIES), config.QUERIES_PATH))
