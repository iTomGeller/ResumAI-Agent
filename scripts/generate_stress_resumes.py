"""Generate 100 diverse Chinese stress-test resumes for end-to-end and RAG evaluation.

Design notes
------------
* Each resume is written as EITHER a ``.pdf`` OR a ``.txt`` file (~50% each).
* ``.txt`` files carry the *real* UTF-8 Chinese resume.
* ``.pdf`` files are produced with :func:`generate_resume_dataset.write_simple_pdf`,
  which emits a text-based (PDFBox-parseable) PDF but is latin-1 encoded, so Chinese
  cannot be embedded. For PDF resumes we therefore write an English/pinyin *equivalent*
  of comparable magnitude. This way both the PDF parsing path and the Chinese path get
  exercised under load.
* ``textLength`` in the manifest is ALWAYS the length of the canonical Chinese resume
  (per spec: "textLength 要按原始中文长度算"), regardless of which file type was emitted.
* ~70% of resumes embed a ``https://github.com/...`` link to exercise GitHub enrichment.

Usage
-----
    python scripts/generate_stress_resumes.py            # 100 resumes, seed 42
    python scripts/generate_stress_resumes.py 100 7      # 100 resumes, seed 7
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "testdata" / "stress_resumes"

# Reuse the existing text-based PDF writer (PDFBox-parseable, latin-1 encoded).
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from generate_resume_dataset import write_simple_pdf  # noqa: E402  (intentional reuse)


# ---------------------------------------------------------------------------
# Shared data pools
# ---------------------------------------------------------------------------
SURNAMES = [
    ("张", "Zhang"), ("王", "Wang"), ("李", "Li"), ("赵", "Zhao"), ("陈", "Chen"),
    ("刘", "Liu"), ("杨", "Yang"), ("黄", "Huang"), ("周", "Zhou"), ("吴", "Wu"),
    ("徐", "Xu"), ("孙", "Sun"), ("胡", "Hu"), ("朱", "Zhu"), ("高", "Gao"),
    ("林", "Lin"), ("何", "He"), ("郭", "Guo"), ("马", "Ma"), ("罗", "Luo"),
    ("梁", "Liang"), ("宋", "Song"), ("郑", "Zheng"), ("谢", "Xie"), ("韩", "Han"),
    ("唐", "Tang"), ("冯", "Feng"), ("董", "Dong"), ("程", "Cheng"), ("曹", "Cao"),
]
GIVEN_M = [
    ("伟", "Wei"), ("强", "Qiang"), ("磊", "Lei"), ("军", "Jun"), ("勇", "Yong"),
    ("杰", "Jie"), ("涛", "Tao"), ("明", "Ming"), ("超", "Chao"), ("鹏", "Peng"),
    ("浩然", "Haoran"), ("一鸣", "Yiming"), ("子轩", "Zixuan"), ("嘉豪", "Jiahao"),
    ("思远", "Siyuan"), ("文博", "Wenbo"), ("义健", "Yijian"), ("宇航", "Yuhang"),
]
GIVEN_F = [
    ("芳", "Fang"), ("敏", "Min"), ("静", "Jing"), ("丽", "Li"), ("燕", "Yan"),
    ("婷", "Ting"), ("雪", "Xue"), ("颖", "Ying"), ("雨欣", "Yuxin"), ("梦瑶", "Mengyao"),
    ("思琪", "Siqi"), ("欣怡", "Xinyi"), ("雅婷", "Yating"), ("子涵", "Zihan"),
]
SCHOOLS = [
    ("武汉大学", "Wuhan University"),
    ("华中科技大学", "Huazhong University of Science and Technology"),
    ("浙江大学", "Zhejiang University"),
    ("南京大学", "Nanjing University"),
    ("上海交通大学", "Shanghai Jiao Tong University"),
    ("北京邮电大学", "Beijing University of Posts and Telecommunications"),
    ("电子科技大学", "University of Electronic Science and Technology of China"),
    ("西安电子科技大学", "Xidian University"),
    ("哈尔滨工业大学", "Harbin Institute of Technology"),
    ("中山大学", "Sun Yat-sen University"),
    ("四川大学", "Sichuan University"),
    ("山东大学", "Shandong University"),
    ("华南理工大学", "South China University of Technology"),
    ("北京理工大学", "Beijing Institute of Technology"),
    ("同济大学", "Tongji University"),
    ("东南大学", "Southeast University"),
    ("大连理工大学", "Dalian University of Technology"),
    ("中南大学", "Central South University"),
    ("湖南大学", "Hunan University"),
    ("重庆大学", "Chongqing University"),
]
MAJORS = [
    ("软件工程", "Software Engineering"),
    ("计算机科学与技术", "Computer Science and Technology"),
    ("信息安全", "Information Security"),
    ("数据科学与大数据技术", "Data Science and Big Data Technology"),
    ("人工智能", "Artificial Intelligence"),
    ("电子信息工程", "Electronic Information Engineering"),
    ("通信工程", "Communication Engineering"),
    ("网络工程", "Network Engineering"),
]
CITIES = [
    ("北京", "Beijing"), ("上海", "Shanghai"), ("深圳", "Shenzhen"), ("杭州", "Hangzhou"),
    ("广州", "Guangzhou"), ("成都", "Chengdu"), ("武汉", "Wuhan"), ("南京", "Nanjing"),
    ("西安", "Xian"), ("苏州", "Suzhou"), ("厦门", "Xiamen"),
]
BIG_TECH = [
    ("字节跳动", "ByteDance"), ("阿里巴巴", "Alibaba"), ("腾讯", "Tencent"),
    ("美团", "Meituan"), ("百度", "Baidu"), ("京东", "JD.com"), ("网易", "NetEase"),
    ("滴滴出行", "Didi"), ("快手", "Kuaishou"), ("小米", "Xiaomi"),
    ("拼多多", "Pinduoduo"), ("蚂蚁集团", "Ant Group"), ("携程", "Trip.com"),
    ("哔哩哔哩", "Bilibili"), ("华为", "Huawei"),
]
MID = [
    ("货拉拉", "Lalamove"), ("贝壳找房", "Beike"), ("知乎", "Zhihu"),
    ("小红书", "Xiaohongshu"), ("蔚来", "NIO"), ("理想汽车", "Li Auto"),
    ("某金融科技公司", "a fintech company"), ("某企业服务SaaS公司", "a SaaS company"),
]
COURSES_CN = [
    "数据结构与算法", "操作系统", "计算机网络", "数据库系统", "计算机组成原理",
    "软件工程", "分布式系统", "编译原理", "机器学习", "设计模式",
]
COURSES_EN = [
    "Data Structures and Algorithms", "Operating Systems", "Computer Networks",
    "Database Systems", "Computer Organization", "Software Engineering",
    "Distributed Systems", "Compilers", "Machine Learning", "Design Patterns",
]
SELF_CN = [
    "具备扎实的工程基础与较强的问题定位能力，能独立负责模块从设计到上线的全流程",
    "乐于复盘与总结，注重代码质量与可观测性，习惯用数据驱动技术决策",
    "沟通协作能力强，能与产品、测试、运维高效配合，推动项目按期交付",
    "对新技术保持好奇心，持续学习大模型与云原生相关方向并应用于实践",
    "抗压能力强，能在高并发与线上故障场景下保持冷静并快速恢复服务",
    "注重业务价值，善于在技术方案与交付节奏之间做出合理权衡",
]
SELF_EN = [
    "Solid engineering fundamentals and strong troubleshooting skills, able to own a module end to end",
    "Enjoys retrospectives, cares about code quality and observability, and makes data-driven decisions",
    "Strong communication and collaboration with product, QA and ops to deliver projects on time",
    "Curious about new tech, continuously learning LLM and cloud-native topics and applying them in practice",
    "Stays calm under pressure and recovers services quickly during high-concurrency incidents",
    "Business-minded, good at balancing technical design against delivery timelines",
]
PAD_CN = [
    "熟悉 Git 协作流程与 Code Review 规范，重视提交质量",
    "了解 CI/CD 流水线与自动化部署，能编写基础脚本",
    "具备良好的英文技术文档阅读能力，跟进社区最新实践",
    "熟悉常见设计模式与重构手法，关注可维护性",
    "了解阿里云/AWS 等云服务的基础组件与部署方式",
    "熟悉 Linux 常用命令与 Shell 脚本，能进行基础排障",
    "参与过线上故障应急与复盘，沉淀过排障文档",
    "了解领域驱动设计（DDD）思想并在项目中尝试落地",
]
PAD_EN = [
    "Familiar with Git workflows and code review conventions",
    "Knows CI/CD pipelines and automated deployment, can write basic scripts",
    "Comfortable reading English technical docs and tracking community practices",
    "Familiar with common design patterns and refactoring techniques",
    "Knows basic cloud components and deployment on Alibaba Cloud / AWS",
    "Familiar with common Linux commands and shell scripting for triage",
    "Participated in production incident response and wrote runbooks",
    "Knows Domain-Driven Design and has applied it in projects",
]

# Natural "outcome" clauses appended to a portion of bullets to lengthen them and
# add variety, so repeated archetypes do not read identically.
EXP_SUFFIX_CN = [
    "，相关指标得到明显改善并保持稳定",
    "，并沉淀为团队可复用的最佳实践",
    "，有效保障了线上服务稳定性",
    "，获得业务方与团队的一致认可",
    "，相关经验整理为内部文档与技术分享",
    "，支撑了业务规模的快速增长",
    "，显著降低了后续维护成本",
    "，并推动相关规范在团队内落地",
    "，端到端交付质量明显提升",
    "，相关方案在多个业务线复用",
]
EXP_SUFFIX_EN = [
    ", and key metrics improved steadily",
    ", and it became a reusable team best practice",
    ", ensuring online service stability",
    ", recognized by stakeholders and peers",
    ", with experience documented and shared internally",
    ", supporting rapid business growth",
    ", significantly lowering maintenance cost",
    ", and related standards were adopted by the team",
    ", improving end-to-end delivery quality",
    ", reused across multiple business lines",
]

# Metric-rich highlight templates, filled with a role-relevant keyword ({kw}) and
# randomized numbers. These add realistic, retrieval-friendly content per resume.
HIGHLIGHT_CN = [
    "主导的{kw}相关工作累计带来约 {pct}% 的效率提升，并在团队内推广复用",
    "负责的核心模块稳定支撑日均 {daily}万 次访问，可用性长期保持在 99.9% 以上",
    "通过对{kw}的持续优化，将关键链路耗时从 {p99a}ms 降至 {p99b}ms",
    "推动{kw}方向的专项治理，使相关问题数量下降约 {pct2}%",
    "在{kw}方面沉淀了一套可复用的方法论与文档，被多个团队借鉴",
    "结合{kw}完成一次重要技术攻坚，系统在峰值 {qps} QPS 下保持稳定",
    "围绕{kw}建立度量与复盘机制，持续推动交付质量提升",
]
HIGHLIGHT_EN = [
    "Work around {kw} delivered about {pct}% efficiency gains and was reused across the team",
    "Owned core modules that steadily served {daily}0K daily visits with 99.9%+ availability",
    "Continuously optimized {kw}, cutting key-path latency from {p99a}ms to {p99b}ms",
    "Drove a focused effort on {kw}, reducing related issues by about {pct2}%",
    "Built a reusable methodology and docs around {kw}, adopted by several teams",
    "Led a major technical effort involving {kw}, staying stable at {qps} QPS peak",
    "Established metrics and reviews around {kw} to keep improving delivery quality",
]

# Universal, cross-role capability phrases blended with each family's own domain
# keywords (see make_pad_pool) to synthesize varied, realistic supplementary skill
# lines that stay on-topic for the role (no two identical filler lines).
NEUTRAL_TECH_CN = [
    "Git 协作与代码评审", "Linux 与 Shell 脚本基础", "CI/CD 流水线", "RESTful API 设计",
    "单元测试与质量保障", "常见设计模式与重构", "计算机网络基础", "跨团队沟通与协作",
    "需求分析与文档撰写", "数据分析与指标拆解", "问题定位与故障复盘", "项目管理与排期",
]
NEUTRAL_TECH_EN = [
    "Git collaboration and code review", "Linux and shell basics", "CI/CD pipelines",
    "RESTful API design", "unit testing and quality", "design patterns and refactoring",
    "computer networking basics", "cross-team communication", "requirement analysis and documentation",
    "data analysis and metrics", "root-cause analysis and postmortems", "project management and planning",
]
PAD_STRIP_VERBS_CN = ("熟悉", "掌握", "了解", "熟练掌握")
PAD_VERBS_CN = ["熟悉", "掌握", "深入理解", "有实际项目经验使用", "了解并实践过", "能够独立运用"]
PAD_CTX_CN = [
    "并能结合业务场景进行优化", "在高并发与大数据量场景下保障稳定性", "并具备相应的排障与调优能力",
    "能够快速定位与解决线上问题", "并形成可复用的实践经验", "在生产环境中验证过其可靠性",
]
PAD_VERBS_EN = ["Familiar with", "Proficient in", "Deep understanding of", "Hands-on experience with", "Practiced with", "Able to independently use"]
PAD_CTX_EN = [
    "and can optimize for business scenarios", "ensuring stability under high concurrency and large data volumes",
    "with strong troubleshooting and tuning skills", "able to quickly locate and fix online issues",
    "forming reusable practical experience", "validated for reliability in production",
]


# ---------------------------------------------------------------------------
# Family templates (experience bullet pools + project pools + skill phrases)
# Numeric placeholders ({pct},{pct2},{p99a},{p99b},{qps},{daily},{cnt},{gb})
# are filled per-resume so repeated archetypes differ.
# ---------------------------------------------------------------------------
FAMILIES = {
    "backend": {
        "exp_cn": [
            "负责核心交易与订单服务的后端开发，将单体应用按领域拆分为订单、结算、风控等微服务",
            "排查线上实例内存 rss 持续升高问题，结合 GC 日志、heap dump 与 arthas 定位缓存未设上限，内存下降 {pct}%",
            "优化核心接口 file sort 慢 SQL，通过 gh-ost 在线 DDL 增加联合索引，P99 从 {p99a}ms 降到 {p99b}ms",
            "设计基于 Kafka 的异步交易管道，引入幂等键、重试队列与死信处理，峰值支撑 {qps} QPS",
            "搭建服务可观测体系，覆盖接口耗时、错误率、JVM GC、线程池与消费堆积，接入 Prometheus 与 Grafana",
            "排查机房日志采集故障，定位日志 agent 背压导致丢失，调整批量与缓冲策略后恢复稳定",
            "重构配置与模板管理服务，支持动态模板热替换，发布无需重启，发布时长缩短 {pct2}%",
            "优化 Redis 缓存策略，处理热点 key 与缓存击穿，引入本地缓存与分布式锁，命中率提升至九成以上",
            "主导线上故障复盘，输出根因分析与改进项，推动建立告警分级与 oncall 值班机制",
            "日均处理 {daily}万 次请求，保障核心链路 SLA 稳定在 99.9% 以上",
        ],
        "exp_en": [
            "Owned backend of core transaction and order services, splitting the monolith into order, settlement and risk-control microservices",
            "Investigated rising instance memory rss using GC logs, heap dump and arthas, found an unbounded cache and cut memory by {pct}%",
            "Optimized a file-sort slow SQL on a hot API, added a composite index via gh-ost online DDL, reducing P99 from {p99a}ms to {p99b}ms",
            "Designed a Kafka-based async transaction pipeline with idempotency keys, retry queues and dead-letter handling, sustaining {qps} QPS at peak",
            "Built service observability covering API latency, error rate, JVM GC, thread pools and consumer backlog with Prometheus and Grafana",
            "Fixed a log-collection failure caused by agent backpressure, restoring stability after tuning batch and buffer settings",
            "Rebuilt the config and template management service to support hot template reload without restart, cutting release time by {pct2}%",
            "Tuned Redis caching for hot keys and cache breakdown with local cache and distributed locks, raising hit rate above 90%",
            "Led a production incident review with root-cause analysis and drove an alert-tiering and on-call rotation mechanism",
            "Served {daily}0K requests per day while keeping core-path SLA above 99.9%",
        ],
        "skills_cn": [
            "熟悉分布式事务与一致性方案", "掌握 MySQL 索引与慢查询优化", "熟悉 Redis 缓存与分布式锁",
            "了解 Kafka/RocketMQ 消息中间件", "熟悉 JVM 调优与故障排查", "掌握 Docker 与容器化部署",
        ],
        "skills_en": [
            "Distributed transactions and consistency", "MySQL indexing and slow-query tuning",
            "Redis caching and distributed locks", "Kafka/RocketMQ messaging",
            "JVM tuning and troubleshooting", "Docker and containerization",
        ],
        "projects": [
            {
                "name_cn": "分布式支付结算平台", "name_en": "Distributed Payment & Settlement Platform",
                "stack_cn": "Spring Boot + MySQL + Kafka + Redis", "stack_en": "Spring Boot + MySQL + Kafka + Redis",
                "bul_cn": [
                    "实现支付网关、内部账本与结算文件之间的每日对账，自动发现并冲正差异交易",
                    "为每一笔支付状态流转记录审计 trace id，便于合规排查与问题回溯",
                    "通过分库分表与读写分离支撑交易量增长，单表数据量控制在千万级以内",
                    "引入幂等与最终一致性方案，保证重试场景下不重复扣款",
                ],
                "bul_en": [
                    "Implemented daily reconciliation across payment gateway, ledger and settlement files with auto-correction",
                    "Recorded an audit trace id for every payment state transition for compliance",
                    "Used sharding and read-write splitting to keep single-table rows within tens of millions",
                    "Applied idempotency and eventual consistency to avoid duplicate charges on retry",
                ],
            },
            {
                "name_cn": "高并发秒杀与库存系统", "name_en": "High-Concurrency Flash-Sale & Inventory System",
                "stack_cn": "Java + Redis + RocketMQ", "stack_en": "Java + Redis + RocketMQ",
                "bul_cn": [
                    "采用 Redis 预扣库存 + 消息异步落库，削峰填谷应对瞬时高并发",
                    "设计分布式锁与令牌桶限流，防止超卖与刷单",
                    "压测峰值 {qps} QPS 下系统稳定，错误率低于千分之一",
                    "为下游故障设计熔断降级与兜底策略",
                ],
                "bul_en": [
                    "Pre-deducted inventory in Redis with async DB writes to absorb traffic spikes",
                    "Designed distributed locks and token-bucket rate limiting to prevent oversell",
                    "Stayed stable at {qps} QPS in load tests with error rate below 0.1%",
                    "Built circuit breaking and fallback for downstream failures",
                ],
            },
            {
                "name_cn": "企业级权限与网关中台", "name_en": "Enterprise Auth & Gateway Platform",
                "stack_cn": "Spring Cloud Gateway + OAuth2", "stack_en": "Spring Cloud Gateway + OAuth2",
                "bul_cn": [
                    "统一鉴权、限流、灰度路由与链路追踪，沉淀为可复用的中台能力",
                    "对接 OAuth2 与 RBAC 权限模型，支持多租户隔离",
                    "网关层接入熔断降级，故障时自动隔离异常下游",
                    "提供统一 SDK，降低各业务线接入成本",
                ],
                "bul_en": [
                    "Unified auth, rate limiting, gray routing and tracing as reusable platform capabilities",
                    "Integrated OAuth2 and RBAC with multi-tenant isolation",
                    "Added circuit breaking to isolate faulty downstreams",
                    "Shipped a unified SDK to lower integration cost across teams",
                ],
            },
        ],
    },
    "agent": {
        "exp_cn": [
            "负责多智能体简历评估工作流的后端开发，串联意图路由、解析、JD 匹配、技术评估、风险评估与报告生成节点",
            "设计 Agent 运行时框架，包含路由计划、工具预算、策略护栏、轨迹回放与评测反馈闭环",
            "实现混合 RAG 检索，融合 BM25 词法召回、向量检索、重排与兜底可见性，证据命中率提升 {pct}%",
            "接入 MCP 简历证据服务与动态技能加载，实现证据合成与工具治理",
            "将大模型调用全链路接入 Langfuse 追踪，记录每一步的命中数、TopScore、兜底率与时延",
            "对 LLM 输出做结构化约束与校验，降低幻觉，关键字段抽取准确率提升至九成以上",
            "优化向量库 Milvus 的索引与分片策略，检索时延从 {p99a}ms 降到 {p99b}ms",
            "设计提示词模板与版本管理，支持灰度与 A/B，迭代效率提升 {pct2}%",
            "搭建离线评测集与自动化评分，覆盖忠实度、相关性与可用性等指标",
            "日均处理 {daily}万 次智能体调用，保障在线服务稳定性",
        ],
        "exp_en": [
            "Built backend of a multi-agent resume evaluation workflow chaining intent router, parser, JD matcher, technical/risk evaluators and report generator",
            "Designed an agent runtime with route plan, tool budget, policy guardrails, trace replay and an evaluation feedback loop",
            "Implemented hybrid RAG fusing BM25 lexical recall, vector search, reranking and fallback visibility, raising evidence hit rate by {pct}%",
            "Integrated an MCP resume-evidence service and dynamic skill loading for evidence synthesis and tool governance",
            "Wired the full LLM call chain into Langfuse, tracking hit count, top score, fallback rate and latency per step",
            "Added structured constraints and validation on LLM output to reduce hallucination, raising key-field extraction accuracy above 90%",
            "Tuned Milvus index and sharding strategy, reducing retrieval latency from {p99a}ms to {p99b}ms",
            "Designed prompt templates with version management supporting gray release and A/B, improving iteration speed by {pct2}%",
            "Built an offline evaluation set and automated scoring covering faithfulness, relevance and usability",
            "Handled {daily}0K agent invocations per day while keeping the online service stable",
        ],
        "skills_cn": [
            "熟悉 LangGraph/LangChain 智能体编排", "掌握 RAG 检索与重排", "熟悉 Milvus 向量数据库",
            "了解 Prompt 工程与评测", "熟悉 Spring AI / FastAPI 服务化", "了解 LLM 可观测与追踪",
        ],
        "skills_en": [
            "LangGraph/LangChain agent orchestration", "RAG retrieval and reranking",
            "Milvus vector database", "Prompt engineering and evaluation",
            "Spring AI / FastAPI services", "LLM observability and tracing",
        ],
        "projects": [
            {
                "name_cn": "ResumAI Agent 智能简历评估平台", "name_en": "ResumAI Agent - Intelligent Resume Evaluation Platform",
                "stack_cn": "Spring Boot + Vue3 + Milvus + Neo4j + DeepSeek", "stack_en": "Spring Boot + Vue3 + Milvus + Neo4j + DeepSeek",
                "bul_cn": [
                    "基于 DAG 编排多智能体评估流程，覆盖解析、匹配、评估与报告生成",
                    "实现 RAG 证据检索与引用溯源，让评估结论可追溯",
                    "基于 Neo4j 构建技能图谱，关联岗位与候选人能力",
                    "接入 Prometheus + Grafana 实现全链路可观测",
                ],
                "bul_en": [
                    "Orchestrated a multi-agent evaluation flow with a DAG covering parsing, matching, evaluation and report generation",
                    "Implemented RAG evidence retrieval with citation tracing for explainable conclusions",
                    "Built a Neo4j knowledge graph linking job skills and candidate abilities",
                    "Integrated Prometheus and Grafana for full-link observability",
                ],
            },
            {
                "name_cn": "企业知识库问答系统", "name_en": "Enterprise Knowledge Base QA",
                "stack_cn": "LangChain + Milvus + Redis", "stack_en": "LangChain + Milvus + Redis",
                "bul_cn": [
                    "实现文档切分与多路召回，提升长文档问答质量",
                    "引入重排与引用溯源，降低答非所问",
                    "支持多轮对话记忆与上下文管理",
                    "建设在线评测与反馈回流机制",
                ],
                "bul_en": [
                    "Implemented document chunking and multi-route recall to improve long-document QA",
                    "Added reranking and citation tracing to reduce off-topic answers",
                    "Supported multi-turn conversation memory and context management",
                    "Built online evaluation and a feedback loop",
                ],
            },
            {
                "name_cn": "智能客服 Agent", "name_en": "Customer Service Agent",
                "stack_cn": "FastAPI + LangGraph", "stack_en": "FastAPI + LangGraph",
                "bul_cn": [
                    "设计工具编排与函数调用，打通业务系统",
                    "实现意图识别与槽位填充，提升解决率",
                    "设计人工兜底与升级流程，保障体验",
                    "用户满意度提升 {pct}%",
                ],
                "bul_en": [
                    "Designed tool orchestration and function calling to connect business systems",
                    "Implemented intent recognition and slot filling to raise resolution rate",
                    "Designed human fallback and escalation for better experience",
                    "Improved user satisfaction by {pct}%",
                ],
            },
        ],
    },
    "frontend": {
        "exp_cn": [
            "负责后台管理与数据看板的前端开发，搭建候选人列表、报告详情、轨迹时间线与反馈管理等页面",
            "优化首屏加载与长列表渲染，引入虚拟滚动与懒加载，首屏时间下降 {pct}%",
            "沉淀通用组件库与脚手架，统一交互与样式规范，组件复用率提升至 {pct2}%",
            "基于 ECharts 实现复杂可视化图表，支持大数据量下的流畅交互",
            "接入埋点与前端监控，采集白屏、JS 异常与接口耗时，建立前端质量看板",
            "推动工程化改造并迁移到 Vite，本地启动与构建明显提速",
            "与后端约定接口契约，封装统一请求层与错误处理，提升联调效率",
            "实现权限路由与多主题切换，支持暗色模式与国际化",
            "负责微前端拆分与子应用接入，降低团队间耦合",
            "编写单元测试与端到端测试，保障核心交互稳定",
        ],
        "exp_en": [
            "Built admin and dashboard frontends including candidate list, report detail, trace timeline and feedback pages",
            "Optimized first-screen load and long-list rendering with virtual scrolling and lazy loading, cutting first-screen time by {pct}%",
            "Built a shared component library and scaffolding, unifying interaction and style specs and raising reuse to {pct2}%",
            "Implemented complex ECharts visualizations with smooth interaction over large datasets",
            "Added tracking and frontend monitoring for blank screen, JS errors and API latency with a quality dashboard",
            "Drove engineering migration to Vite, significantly speeding up local startup and builds",
            "Defined API contracts with backend and wrapped a unified request layer and error handling",
            "Implemented permission routing and multi-theme switching with dark mode and i18n",
            "Split a micro-frontend and onboarded sub-apps to reduce cross-team coupling",
            "Wrote unit and end-to-end tests to keep core interactions stable",
        ],
        "skills_cn": [
            "熟悉 Vue3 与组合式 API", "掌握 TypeScript 与工程化", "熟悉 ECharts 数据可视化",
            "了解 React 与微前端", "熟悉前端性能优化", "掌握 Vite/Webpack 构建",
        ],
        "skills_en": [
            "Vue3 and Composition API", "TypeScript and tooling", "ECharts data visualization",
            "React and micro-frontends", "Frontend performance optimization", "Vite/Webpack builds",
        ],
        "projects": [
            {
                "name_cn": "智能简历评估平台前端", "name_en": "Resume Evaluation Platform Frontend",
                "stack_cn": "Vue3 + TypeScript + Vite + Pinia", "stack_en": "Vue3 + TypeScript + Vite + Pinia",
                "bul_cn": [
                    "搭建候选人列表、报告详情与轨迹时间线页面",
                    "优化首屏与长列表渲染，提升交互流畅度",
                    "沉淀通用组件库与脚手架",
                    "接入埋点与前端监控",
                ],
                "bul_en": [
                    "Built candidate list, report detail and trace timeline pages",
                    "Optimized first-screen and long-list rendering for smoother interaction",
                    "Built a reusable component library and scaffolding",
                    "Added tracking and frontend monitoring",
                ],
            },
            {
                "name_cn": "实时数据可视化大屏", "name_en": "Realtime Data Visualization Dashboard",
                "stack_cn": "Vue3 + ECharts + WebSocket", "stack_en": "Vue3 + ECharts + WebSocket",
                "bul_cn": [
                    "渲染大数据量的复杂图表并保持流畅",
                    "基于 WebSocket 实现数据实时更新",
                    "支持主题切换与暗色模式",
                    "优化重绘与内存占用",
                ],
                "bul_en": [
                    "Rendered complex charts over large datasets while staying smooth",
                    "Implemented live updates over WebSocket",
                    "Supported theme switching and dark mode",
                    "Optimized repaint and memory usage",
                ],
            },
            {
                "name_cn": "组件库与设计系统", "name_en": "Component Library & Design System",
                "stack_cn": "Vue3 + Storybook", "stack_en": "Vue3 + Storybook",
                "bul_cn": [
                    "统一交互与样式规范",
                    "使用 Storybook 编写组件文档",
                    "提升跨团队复用率",
                    "补充单元测试与端到端测试",
                ],
                "bul_en": [
                    "Unified interaction and style specs",
                    "Documented components with Storybook",
                    "Improved reuse across teams",
                    "Added unit and e2e tests",
                ],
            },
        ],
    },
    "product": {
        "exp_cn": [
            "负责 AI 简历筛选产品全流程设计，覆盖上传、解析、评估、报告复核与 HR 反馈闭环",
            "定义核心指标：筛选时长、推荐采纳率、人工改写率与证据覆盖率，驱动迭代",
            "主导需求评审与 PRD 撰写，协调研发、数据与客户成功团队推进企业级落地",
            "设计大模型报告质量反馈体系与评测看板，持续提升生成质量",
            "通过用户访谈与数据分析挖掘痛点，规划产品路线图",
            "设计 A/B 实验验证关键功能，核心转化率提升 {pct}%",
            "推动商业化方案与定价策略，季度营收增长 {pct2}%",
            "负责竞品分析与行业调研，输出差异化竞争策略",
        ],
        "exp_en": [
            "Owned end-to-end design of an AI resume-screening product from upload to HR feedback",
            "Defined core metrics: time-to-screen, recommendation acceptance, manual override and evidence coverage",
            "Led requirement reviews and PRDs, coordinating engineering, data and CS teams for enterprise rollout",
            "Designed an LLM report-quality feedback system and evaluation dashboard to lift generation quality",
            "Mined pain points via user interviews and data analysis and shaped the roadmap",
            "Ran A/B experiments validating key features, lifting core conversion by {pct}%",
            "Drove monetization and pricing, growing quarterly revenue by {pct2}%",
            "Owned competitive analysis and market research with a differentiation strategy",
        ],
        "skills_cn": [
            "熟悉需求分析与 PRD 撰写", "掌握数据分析与指标体系", "熟悉 A/B 实验设计",
            "了解大模型应用边界", "熟悉项目管理与跨团队协作", "了解基础 SQL 取数",
        ],
        "skills_en": [
            "Requirement analysis and PRD writing", "Data analysis and metric systems",
            "A/B experiment design", "LLM application boundaries",
            "Project management and cross-team collaboration", "Basic SQL for data pulls",
        ],
        "projects": [
            {
                "name_cn": "AI 简历筛选工作流", "name_en": "AI Resume Screening Workflow",
                "stack_cn": "B2B SaaS 工作流", "stack_en": "B2B SaaS",
                "bul_cn": [
                    "设计上传到反馈的端到端流程",
                    "定义核心指标与数据看板",
                    "协调研发、数据与客户成功团队",
                    "推动企业级客户落地",
                ],
                "bul_en": [
                    "Designed the end-to-end flow from upload to feedback",
                    "Defined core metrics and dashboards",
                    "Coordinated engineering, data and CS teams",
                    "Drove enterprise customer rollout",
                ],
            },
            {
                "name_cn": "大模型报告质量评测体系", "name_en": "LLM Report Quality Evaluation System",
                "stack_cn": "大模型评测", "stack_en": "LLM Evaluation",
                "bul_cn": [
                    "设计反馈分类体系",
                    "搭建评测看板",
                    "提升生成报告质量",
                    "形成质量改进闭环",
                ],
                "bul_en": [
                    "Designed a feedback taxonomy",
                    "Built an evaluation dashboard",
                    "Improved generated report quality",
                    "Closed the quality-improvement loop",
                ],
            },
            {
                "name_cn": "企业招聘数据分析平台", "name_en": "Recruiting Data Analytics Platform",
                "stack_cn": "数据分析", "stack_en": "Analytics",
                "bul_cn": [
                    "定义漏斗与转化指标",
                    "设计 A/B 实验",
                    "输出洞察支撑决策",
                    "规划产品路线图",
                ],
                "bul_en": [
                    "Defined funnel and conversion metrics",
                    "Designed A/B experiments",
                    "Delivered insights to support decisions",
                    "Shaped the product roadmap",
                ],
            },
        ],
    },
    "data": {
        "exp_cn": [
            "负责实时数仓与流式 ETL 开发，基于 Flink 处理 Kafka 接入的海量埋点数据",
            "设计分层数仓（ODS/DWD/DWS/ADS）与数据建模，支撑 BI 与算法特征",
            "优化 Spark 离线任务，作业耗时下降 {pct}%，资源成本下降 {pct2}%",
            "搭建数据质量监控与血缘，异常自动告警",
            "治理数据指标口径，统一指标平台，减少口径不一致问题",
            "日均处理 {daily}万 条数据，保障 T+1 与实时链路稳定",
            "优化 Kafka 消费与分区策略，消费堆积明显下降",
            "支撑实时大屏与实时风控特征，端到端时延控制在秒级",
        ],
        "exp_en": [
            "Owned realtime warehouse and streaming ETL with Flink over massive Kafka event data",
            "Designed a layered warehouse (ODS/DWD/DWS/ADS) and data modeling for BI and ML features",
            "Optimized Spark offline jobs, cutting runtime by {pct}% and resource cost by {pct2}%",
            "Built data-quality monitoring and lineage with automatic anomaly alerts",
            "Governed metric definitions on a unified platform to reduce inconsistencies",
            "Processed {daily}0K records per day keeping T+1 and realtime paths stable",
            "Tuned Kafka consumption and partitioning, significantly reducing backlog",
            "Supported realtime dashboards and risk features with second-level end-to-end latency",
        ],
        "skills_cn": [
            "熟悉 Flink 流式计算", "掌握 Spark 离线处理", "熟悉数据仓库建模",
            "了解 Hive/ClickHouse", "熟悉 Kafka 数据接入", "了解数据治理与血缘",
        ],
        "skills_en": [
            "Flink stream processing", "Spark batch processing", "Data warehouse modeling",
            "Hive/ClickHouse", "Kafka data ingestion", "Data governance and lineage",
        ],
        "projects": [
            {
                "name_cn": "实时数据平台", "name_en": "Realtime Data Platform",
                "stack_cn": "Flink + Kafka + ClickHouse", "stack_en": "Flink + Kafka + ClickHouse",
                "bul_cn": [
                    "基于 Flink 处理 Kafka 实时数据，端到端时延控制在秒级",
                    "写入 ClickHouse 支撑实时大屏与即席查询",
                    "设计 Exactly-Once 语义保障数据准确",
                    "日均处理 {daily}万 条事件",
                ],
                "bul_en": [
                    "Processed Kafka realtime data with Flink at second-level end-to-end latency",
                    "Wrote to ClickHouse for realtime dashboards and ad-hoc queries",
                    "Designed exactly-once semantics for data accuracy",
                    "Processed {daily}0K events per day",
                ],
            },
            {
                "name_cn": "离线数仓与指标平台", "name_en": "Offline Warehouse & Metrics Platform",
                "stack_cn": "Spark + Hive", "stack_en": "Spark + Hive",
                "bul_cn": [
                    "基于 Spark 与 Hive 构建分层数仓",
                    "统一指标口径与维度建模",
                    "优化作业调度，耗时下降 {pct}%",
                    "建设数据质量与血缘监控",
                ],
                "bul_en": [
                    "Built a layered warehouse with Spark and Hive",
                    "Unified metric definitions and dimensional modeling",
                    "Optimized scheduling, cutting runtime by {pct}%",
                    "Built data-quality and lineage monitoring",
                ],
            },
        ],
    },
    "infra": {
        "exp_cn": [
            "负责 Kubernetes 集群运维与稳定性保障，管理多套生产环境集群",
            "编写 Helm Chart 与 CI/CD 流水线，实现蓝绿与灰度发布",
            "搭建 Prometheus + Grafana 监控与告警体系，建立 SLO 与错误预算",
            "推动容器化与资源治理，集群资源利用率提升 {pct}%",
            "处理线上故障与容量规划，MTTR 下降 {pct2}%",
            "编写自动化脚本与运维平台，减少人工操作",
            "优化镜像构建与制品管理，构建时长明显下降",
            "负责日志采集（ELK/Loki）与链路追踪体系建设",
        ],
        "exp_en": [
            "Operated Kubernetes clusters and ensured stability across multiple production environments",
            "Wrote Helm charts and CI/CD pipelines enabling blue-green and gray releases",
            "Built Prometheus + Grafana monitoring and alerting with SLOs and error budgets",
            "Drove containerization and resource governance, raising cluster utilization by {pct}%",
            "Handled incidents and capacity planning, reducing MTTR by {pct2}%",
            "Wrote automation scripts and an ops platform to cut manual work",
            "Optimized image builds and artifact management, cutting build time significantly",
            "Built log collection (ELK/Loki) and distributed tracing",
        ],
        "skills_cn": [
            "熟悉 Kubernetes 与容器编排", "掌握 Helm 与 GitOps", "熟悉 Prometheus/Grafana",
            "了解 CI/CD 流水线", "熟悉 Shell/Python 脚本", "了解 SRE 稳定性体系",
        ],
        "skills_en": [
            "Kubernetes and container orchestration", "Helm and GitOps", "Prometheus/Grafana",
            "CI/CD pipelines", "Shell/Python scripting", "SRE reliability practices",
        ],
        "projects": [
            {
                "name_cn": "容器化发布平台", "name_en": "Containerized Release Platform",
                "stack_cn": "Kubernetes + Helm + ArgoCD", "stack_en": "Kubernetes + Helm + ArgoCD",
                "bul_cn": [
                    "基于 Kubernetes 与 Helm 实现标准化发布",
                    "接入 ArgoCD 实现 GitOps",
                    "支持蓝绿与灰度发布",
                    "集群资源利用率提升 {pct}%",
                ],
                "bul_en": [
                    "Standardized releases with Kubernetes and Helm",
                    "Adopted ArgoCD for GitOps",
                    "Supported blue-green and gray releases",
                    "Raised cluster utilization by {pct}%",
                ],
            },
            {
                "name_cn": "统一监控告警平台", "name_en": "Unified Monitoring & Alerting",
                "stack_cn": "Prometheus + Grafana + Alertmanager", "stack_en": "Prometheus + Grafana + Alertmanager",
                "bul_cn": [
                    "基于 Prometheus 采集多维指标",
                    "Grafana 看板与 SLO 管理",
                    "Alertmanager 告警分级与降噪",
                    "MTTR 下降 {pct2}%",
                ],
                "bul_en": [
                    "Collected multi-dimensional metrics with Prometheus",
                    "Grafana dashboards and SLO management",
                    "Alert tiering and noise reduction via Alertmanager",
                    "Reduced MTTR by {pct2}%",
                ],
            },
        ],
    },
    "ml": {
        "exp_cn": [
            "负责推荐/搜索算法的特征工程与模型迭代，线上 CTR 提升 {pct}%",
            "搭建模型训练与服务化流程，支持离线训练与在线推理",
            "基于 PyTorch 实现深度模型，离线指标稳定提升",
            "设计 A/B 实验评估策略效果，建立指标看板",
            "优化特征存储与实时特征，端到端时延下降 {pct2}%",
            "接入 MLflow 管理实验与模型版本",
            "处理样本不均衡与冷启动问题，提升长尾效果",
            "与工程团队协作完成模型上线与监控",
        ],
        "exp_en": [
            "Owned feature engineering and model iteration for recommendation/search, lifting online CTR by {pct}%",
            "Built model training and serving pipelines for offline training and online inference",
            "Implemented deep models with PyTorch with steady offline metric gains",
            "Designed A/B experiments to evaluate strategies with metric dashboards",
            "Optimized feature store and realtime features, cutting end-to-end latency by {pct2}%",
            "Adopted MLflow for experiment and model version management",
            "Handled class imbalance and cold-start to improve long-tail performance",
            "Collaborated with engineering to launch and monitor models",
        ],
        "skills_cn": [
            "熟悉机器学习与深度学习", "掌握 PyTorch 建模", "熟悉特征工程",
            "了解推荐/搜索系统", "熟悉模型服务化", "了解 A/B 实验",
        ],
        "skills_en": [
            "Machine learning and deep learning", "PyTorch modeling", "Feature engineering",
            "Recommendation/search systems", "Model serving", "A/B experimentation",
        ],
        "projects": [
            {
                "name_cn": "推荐排序模型", "name_en": "Recommendation Ranking Model",
                "stack_cn": "PyTorch + Feature Store", "stack_en": "PyTorch + Feature Store",
                "bul_cn": [
                    "设计特征工程与样本构建",
                    "基于 PyTorch 训练排序模型，CTR 提升 {pct}%",
                    "接入特征存储实现实时特征",
                    "通过 A/B 实验验证效果",
                ],
                "bul_en": [
                    "Designed feature engineering and sample construction",
                    "Trained a ranking model with PyTorch, lifting CTR by {pct}%",
                    "Integrated a feature store for realtime features",
                    "Validated gains via A/B experiments",
                ],
            },
            {
                "name_cn": "模型服务平台", "name_en": "Model Serving Platform",
                "stack_cn": "MLflow + Triton", "stack_en": "MLflow + Triton",
                "bul_cn": [
                    "基于 MLflow 管理实验与模型版本",
                    "模型服务化与在线推理",
                    "监控模型效果与漂移",
                    "支持灰度发布",
                ],
                "bul_en": [
                    "Managed experiments and model versions with MLflow",
                    "Served models for online inference",
                    "Monitored model performance and drift",
                    "Supported gray release",
                ],
            },
        ],
    },
    "quality": {
        "exp_cn": [
            "负责接口与 UI 自动化测试框架建设，覆盖核心业务回归",
            "基于 Pytest/Selenium 编写自动化用例，回归效率提升 {pct}%",
            "搭建性能压测体系，定位瓶颈，接口吞吐提升 {pct2}%",
            "接入 CI/CD 实现提交即测，缺陷提前暴露",
            "治理 flaky 用例，提升测试稳定性",
            "设计测试数据与 mock 平台，降低联调成本",
            "推动质量门禁与发布卡点，线上缺陷率下降",
            "输出质量度量看板，量化测试覆盖与缺陷分布",
        ],
        "exp_en": [
            "Built API and UI test-automation frameworks covering core regression",
            "Wrote automation with Pytest/Selenium, improving regression efficiency by {pct}%",
            "Built load-testing, located bottlenecks and raised API throughput by {pct2}%",
            "Integrated CI/CD for test-on-commit to surface defects early",
            "Tamed flaky tests to improve stability",
            "Designed test data and a mock platform to cut integration cost",
            "Drove quality gates and release checkpoints, reducing production defects",
            "Delivered quality dashboards quantifying coverage and defect distribution",
        ],
        "skills_cn": [
            "熟悉自动化测试框架", "掌握 Pytest/Selenium", "熟悉接口与性能测试",
            "了解 CI/CD 集成", "熟悉测试数据与 mock", "了解质量度量体系",
        ],
        "skills_en": [
            "Test automation frameworks", "Pytest/Selenium", "API and performance testing",
            "CI/CD integration", "Test data and mocking", "Quality metric systems",
        ],
        "projects": [
            {
                "name_cn": "自动化测试平台", "name_en": "Test Automation Platform",
                "stack_cn": "Pytest + Selenium + Allure", "stack_en": "Pytest + Selenium + Allure",
                "bul_cn": [
                    "基于 Pytest 与 Selenium 搭建框架",
                    "集成 Allure 测试报告",
                    "接入 CI 实现提交即测",
                    "回归效率提升 {pct}%",
                ],
                "bul_en": [
                    "Built a framework with Pytest and Selenium",
                    "Integrated Allure reporting",
                    "Wired CI for test-on-commit",
                    "Improved regression efficiency by {pct}%",
                ],
            },
            {
                "name_cn": "全链路压测体系", "name_en": "Full-Link Load Testing",
                "stack_cn": "JMeter + Grafana", "stack_en": "JMeter + Grafana",
                "bul_cn": [
                    "基于 JMeter 设计压测场景",
                    "定位性能瓶颈",
                    "Grafana 监控关键指标",
                    "接口吞吐提升 {pct2}%",
                ],
                "bul_en": [
                    "Designed load scenarios with JMeter",
                    "Located performance bottlenecks",
                    "Monitored key metrics with Grafana",
                    "Raised API throughput by {pct2}%",
                ],
            },
        ],
    },
    "security": {
        "exp_cn": [
            "负责后端安全开发与漏洞治理，修复 SQL 注入、越权与 XSS 等风险",
            "设计统一鉴权与权限模型，落地 OAuth2 与 RBAC，支持多租户隔离",
            "建设审计日志与操作留痕，满足合规要求",
            "落地支付风控规则与名单体系，拦截可疑交易，风险拦截率提升 {pct}%",
            "推动密钥管理与数据加密，敏感数据脱敏",
            "配合安全测试与渗透，修复高危漏洞，复发率下降 {pct2}%",
            "建设安全基线与代码扫描，纳入 CI 流程",
            "处理安全应急事件并输出复盘报告",
        ],
        "exp_en": [
            "Owned secure backend development and vulnerability remediation (SQLi, privilege escalation, XSS)",
            "Designed unified auth and permission models with OAuth2 and RBAC and multi-tenant isolation",
            "Built audit logging and operation trails for compliance",
            "Implemented payment risk rules and lists, blocking suspicious transactions and raising interception by {pct}%",
            "Drove key management and data encryption with sensitive-data masking",
            "Worked with security testing/pentest to fix high-risk vulns, cutting recurrence by {pct2}%",
            "Built security baselines and code scanning integrated into CI",
            "Handled security incidents and produced postmortems",
        ],
        "skills_cn": [
            "熟悉常见 Web 安全漏洞", "掌握 OAuth2 与 RBAC", "熟悉审计与合规",
            "了解风控与名单体系", "熟悉数据加密与脱敏", "了解代码扫描与安全基线",
        ],
        "skills_en": [
            "Common web security vulnerabilities", "OAuth2 and RBAC", "Auditing and compliance",
            "Risk control and lists", "Data encryption and masking", "Code scanning and baselines",
        ],
        "projects": [
            {
                "name_cn": "统一鉴权与风控中台", "name_en": "Unified Auth & Risk-Control Platform",
                "stack_cn": "Spring Cloud Gateway + OAuth2", "stack_en": "Spring Cloud Gateway + OAuth2",
                "bul_cn": [
                    "基于网关统一鉴权与限流",
                    "落地 OAuth2 与 RBAC",
                    "建设风控规则与名单体系",
                    "风险拦截率提升 {pct}%",
                ],
                "bul_en": [
                    "Unified auth and rate limiting at the gateway",
                    "Implemented OAuth2 and RBAC",
                    "Built risk rules and lists",
                    "Raised risk interception by {pct}%",
                ],
            },
            {
                "name_cn": "数据安全与审计平台", "name_en": "Data Security & Audit Platform",
                "stack_cn": "Java + 加密 + 审计", "stack_en": "Java + Encryption + Audit",
                "bul_cn": [
                    "敏感数据加密与脱敏",
                    "全量操作审计留痕",
                    "漏洞扫描纳入 CI",
                    "满足等保与合规要求",
                ],
                "bul_en": [
                    "Encrypted and masked sensitive data",
                    "Kept full operation audit trails",
                    "Integrated vuln scanning into CI",
                    "Met compliance requirements",
                ],
            },
        ],
    },
    "mobile": {
        "exp_cn": [
            "负责 Android 客户端开发与架构演进，推进组件化与模块解耦",
            "优化应用启动速度，冷启动时间从 {p99a}ms 降到 {p99b}ms",
            "治理崩溃与 ANR，崩溃率下降 {pct}%",
            "接入支付与音视频 SDK，保障核心链路稳定",
            "优化包体积与内存占用，包体下降 {pct2}%",
            "基于 Jetpack 组件重构页面，提升可维护性",
            "建设端监控与灰度发布能力",
            "与后端协作优化弱网体验",
        ],
        "exp_en": [
            "Owned Android client development and architecture evolution with componentization",
            "Optimized startup, reducing cold-start from {p99a}ms to {p99b}ms",
            "Governed crashes and ANRs, reducing crash rate by {pct}%",
            "Integrated payment and media SDKs while keeping core paths stable",
            "Optimized package size and memory, cutting package size by {pct2}%",
            "Refactored pages with Jetpack components for maintainability",
            "Built device monitoring and gray-release capabilities",
            "Improved weak-network experience with backend",
        ],
        "skills_cn": [
            "熟悉 Android 与 Kotlin", "掌握 Jetpack 组件", "熟悉性能与崩溃治理",
            "了解组件化与插件化", "熟悉音视频/支付 SDK", "了解端监控与灰度",
        ],
        "skills_en": [
            "Android and Kotlin", "Jetpack components", "Performance and crash governance",
            "Componentization and plugins", "Media/payment SDKs", "Device monitoring and gray release",
        ],
        "projects": [
            {
                "name_cn": "电商 Android 客户端", "name_en": "E-commerce Android App",
                "stack_cn": "Kotlin + Jetpack", "stack_en": "Kotlin + Jetpack",
                "bul_cn": [
                    "基于 Kotlin 与 Jetpack 开发",
                    "组件化架构解耦",
                    "优化启动与内存占用",
                    "崩溃率下降 {pct}%",
                ],
                "bul_en": [
                    "Built with Kotlin and Jetpack",
                    "Decoupled via componentized architecture",
                    "Optimized startup and memory",
                    "Reduced crash rate by {pct}%",
                ],
            },
            {
                "name_cn": "组件化与插件化框架", "name_en": "Componentization & Plugin Framework",
                "stack_cn": "Android", "stack_en": "Android",
                "bul_cn": [
                    "设计模块路由与通信",
                    "支持动态化与热修复",
                    "统一基础库与规范",
                    "提升团队协作效率",
                ],
                "bul_en": [
                    "Designed module routing and communication",
                    "Supported dynamic loading and hotfix",
                    "Unified base libraries and conventions",
                    "Improved team collaboration",
                ],
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# Archetypes (15) -> map to a family + level. Counts sum to 100.
# ---------------------------------------------------------------------------
ARCHETYPES = [
    {
        "key": "senior_backend", "family": "backend", "level": "senior", "count": 12,
        "role_cn": "资深后端开发工程师", "role_en": "Senior Backend Engineer",
        "title_base_cn": "后端开发工程师", "title_base_en": "Backend Engineer",
        "exp_heading": "work",
        "expected": ["Java", "Spring Boot", "MySQL", "Redis", "Kafka", "Microservices", "High Concurrency", "Observability", "SQL Optimization", "JVM"],
        "summary_cn": "七年后端开发经验，专注支付与交易系统、分布式架构、高并发与线上故障治理，擅长性能优化与可观测体系建设。",
        "summary_en": "Seven years of backend experience focused on payment/transaction systems, distributed architecture, high concurrency and production incident response.",
    },
    {
        "key": "ai_agent_engineer", "family": "agent", "level": "senior", "count": 10,
        "role_cn": "AI Agent 后端工程师", "role_en": "AI Agent Engineer",
        "title_base_cn": "智能体开发工程师", "title_base_en": "AI Agent Engineer",
        "exp_heading": "work",
        "expected": ["LLM", "RAG", "LangGraph", "MCP", "Milvus", "Agent", "Python", "Spring AI", "Tool Orchestration", "Observability"],
        "summary_cn": "后端工程师，专注生产级 AI Agent 工作流、检索增强（RAG）管道与运行时可观测，擅长多智能体编排与工具治理。",
        "summary_en": "Backend engineer building production AI-agent workflows, RAG pipelines and runtime observability, skilled in multi-agent orchestration and tool governance.",
    },
    {
        "key": "llm_rag_engineer", "family": "agent", "level": "mid", "count": 8,
        "role_cn": "大模型应用工程师", "role_en": "LLM Application Engineer",
        "title_base_cn": "大模型应用工程师", "title_base_en": "LLM Application Engineer",
        "exp_heading": "work",
        "expected": ["RAG", "Vector Search", "Milvus", "Rerank", "Embedding", "Prompt Engineering", "Evaluation", "LangChain", "Python", "Knowledge Base"],
        "summary_cn": "大模型应用工程师，专注 RAG 检索、向量库优化、重排与离线评测，擅长把检索质量做成可量化、可迭代的体系。",
        "summary_en": "LLM application engineer focused on RAG retrieval, vector store optimization, reranking and offline evaluation.",
    },
    {
        "key": "junior_frontend", "family": "frontend", "level": "junior", "count": 7,
        "role_cn": "初级前端开发工程师", "role_en": "Junior Frontend Engineer",
        "title_base_cn": "前端开发工程师", "title_base_en": "Frontend Engineer",
        "exp_heading": "work",
        "expected": ["Vue3", "TypeScript", "Vite", "Pinia", "CSS", "ECharts", "Componentization", "HTTP"],
        "summary_cn": "一年半前端开发经验，专注 Vue 后台与数据看板、组件复用与交互细节，乐于打磨页面体验。",
        "summary_en": "1.5 years of frontend experience focused on Vue admin dashboards, component reuse and interaction details.",
    },
    {
        "key": "senior_frontend", "family": "frontend", "level": "senior", "count": 7,
        "role_cn": "资深前端开发工程师", "role_en": "Senior Frontend Engineer",
        "title_base_cn": "前端开发工程师", "title_base_en": "Frontend Engineer",
        "exp_heading": "work",
        "expected": ["Vue3", "React", "TypeScript", "Performance", "Engineering", "Micro-frontend", "Visualization", "Node.js", "Webpack", "Vite"],
        "summary_cn": "资深前端工程师，擅长复杂数据看板、可视化、前端性能优化与工程化，主导过微前端拆分与组件库建设。",
        "summary_en": "Senior frontend engineer skilled in complex dashboards, visualization, performance optimization and tooling, having led micro-frontend splits.",
    },
    {
        "key": "product_manager", "family": "product", "level": "mid", "count": 8,
        "role_cn": "产品经理（AI 方向）", "role_en": "AI Product Manager",
        "title_base_cn": "产品经理", "title_base_en": "Product Manager",
        "exp_heading": "work",
        "expected": ["Requirement Analysis", "PRD", "Data Analysis", "User Research", "A/B Testing", "Project Management", "LLM Application", "Monetization"],
        "summary_cn": "六年产品经验，专注 B2B 工作流产品与大模型功能，擅长数据驱动迭代与跨团队协作推动企业级落地。",
        "summary_en": "Six years of product experience in B2B workflow products and LLM features, skilled in data-driven iteration and cross-team delivery.",
    },
    {
        "key": "data_platform", "family": "data", "level": "mid", "count": 7,
        "role_cn": "数据平台开发工程师", "role_en": "Data Platform Engineer",
        "title_base_cn": "数据开发工程师", "title_base_en": "Data Engineer",
        "exp_heading": "work",
        "expected": ["Flink", "Spark", "Kafka", "Data Warehouse", "ETL", "Hive", "Data Governance", "Python"],
        "summary_cn": "数据平台工程师，专注实时数仓、流式 ETL 与离线计算，擅长数据建模、指标治理与数据质量体系建设。",
        "summary_en": "Data platform engineer focused on realtime warehouse, streaming ETL and batch processing with strong data modeling and governance.",
    },
    {
        "key": "devops_sre", "family": "infra", "level": "mid", "count": 7,
        "role_cn": "运维开发工程师 / SRE", "role_en": "DevOps / SRE Engineer",
        "title_base_cn": "运维开发工程师", "title_base_en": "DevOps Engineer",
        "exp_heading": "work",
        "expected": ["Kubernetes", "Docker", "Helm", "CI/CD", "Prometheus", "Grafana", "SRE", "Shell", "Python"],
        "summary_cn": "运维开发工程师，专注 Kubernetes 集群稳定性、CI/CD 与监控告警体系，擅长故障处理与容量规划。",
        "summary_en": "DevOps/SRE engineer focused on Kubernetes stability, CI/CD and monitoring, skilled in incident handling and capacity planning.",
    },
    {
        "key": "algorithm_ml", "family": "ml", "level": "mid", "count": 6,
        "role_cn": "算法工程师", "role_en": "Machine Learning Engineer",
        "title_base_cn": "算法工程师", "title_base_en": "Machine Learning Engineer",
        "exp_heading": "work",
        "expected": ["Python", "PyTorch", "Machine Learning", "Feature Engineering", "Recommendation", "Model Serving", "MLflow", "A/B Testing"],
        "summary_cn": "算法工程师，专注推荐与搜索方向的特征工程与模型迭代，熟悉模型服务化与 A/B 实验评估。",
        "summary_en": "ML engineer focused on recommendation/search feature engineering and model iteration with serving and A/B evaluation.",
    },
    {
        "key": "qa_automation", "family": "quality", "level": "mid", "count": 5,
        "role_cn": "测试开发工程师", "role_en": "QA Automation Engineer",
        "title_base_cn": "测试开发工程师", "title_base_en": "QA Automation Engineer",
        "exp_heading": "work",
        "expected": ["Test Development", "Python", "Selenium", "API Testing", "Performance Testing", "CI/CD", "Quality Assurance", "Pytest"],
        "summary_cn": "测试开发工程师，专注自动化测试框架、接口与性能测试以及质量度量体系，擅长把质量左移到流水线。",
        "summary_en": "QA automation engineer focused on test frameworks, API/performance testing and quality metrics, shifting quality left into the pipeline.",
    },
    {
        "key": "security_backend", "family": "security", "level": "mid", "count": 5,
        "role_cn": "安全开发工程师", "role_en": "Security Engineer",
        "title_base_cn": "安全开发工程师", "title_base_en": "Security Engineer",
        "exp_heading": "work",
        "expected": ["Java", "Secure Development", "OAuth2", "Audit", "Risk Control", "Vulnerability", "Compliance", "Encryption"],
        "summary_cn": "安全开发工程师，专注后端安全、统一鉴权、风控与合规审计，擅长漏洞治理与安全基线建设。",
        "summary_en": "Security engineer focused on backend security, unified auth, risk control and compliance auditing.",
    },
    {
        "key": "mobile_engineer", "family": "mobile", "level": "mid", "count": 5,
        "role_cn": "Android 开发工程师", "role_en": "Android Engineer",
        "title_base_cn": "移动端开发工程师", "title_base_en": "Android Engineer",
        "exp_heading": "work",
        "expected": ["Android", "Kotlin", "Performance", "Crash Governance", "Componentization", "Jetpack", "Payment SDK"],
        "summary_cn": "移动端工程师，专注 Android 架构演进、启动与崩溃治理以及组件化，擅长性能优化与端监控建设。",
        "summary_en": "Android engineer focused on architecture evolution, startup/crash governance and componentization.",
    },
    {
        "key": "new_grad", "family": "backend", "level": "new_grad", "count": 6,
        "role_cn": "应届后端开发工程师", "role_en": "New Graduate Backend Engineer",
        "title_base_cn": "后端开发工程师", "title_base_en": "Backend Engineer",
        "exp_heading": "intern",
        "expected": ["Java", "Spring Boot", "MySQL", "Redis", "Data Structures", "Computer Networks", "Git"],
        "summary_cn": "计算机相关专业应届生，完成两段后端实习，扎实掌握 Java、Spring Boot 与数据库基础，参与过完整项目开发。",
        "summary_en": "New CS graduate with two backend internships, solid Java/Spring Boot and database fundamentals and full project experience.",
    },
    {
        "key": "career_gap", "family": "backend", "level": "gap", "count": 4,
        "role_cn": "后端开发工程师", "role_en": "Backend Engineer",
        "title_base_cn": "后端开发工程师", "title_base_en": "Backend Engineer",
        "exp_heading": "work",
        "expected": ["Java", "Spring", "MySQL", "Backend"],
        "summary_cn": "后端开发工程师，有数年 Java 后端经验，因个人原因有约一年职业空窗期，期间持续自学并参与开源项目，现重返职场。",
        "summary_en": "Backend engineer with several years of Java experience and an ~1-year career gap, self-studied and contributed to open source during the break.",
    },
    {
        "key": "sparse_risk", "family": "sparse", "level": "sparse", "count": 3,
        "role_cn": "后端开发工程师", "role_en": "Backend Engineer",
        "title_base_cn": "后端开发工程师", "title_base_en": "Backend Engineer",
        "exp_heading": "work",
        "expected": ["Java", "Spring Boot"],
        "summary_cn": "信息稀疏 / 高风险简历，用于测试风险识别与证据缺失场景。",
        "summary_en": "Sparse / high-risk resume for testing risk detection and missing-evidence scenarios.",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_sample(pool: list, k: int, rng: random.Random) -> list:
    k = max(0, min(k, len(pool)))
    return rng.sample(pool, k)


def deco(bullet: str, nums: dict, lang: str, rng: random.Random, p: float) -> str:
    """Render a bullet, optionally appending a natural outcome clause for length/variety."""
    text = bullet.format(**nums)
    if rng.random() < p:
        text += rng.choice(EXP_SUFFIX_CN if lang == "cn" else EXP_SUFFIX_EN)
    text += "。" if lang == "cn" else "."
    return "- " + text


def _family_tech(fam: dict, lang: str) -> list[str]:
    """Domain keyword nouns derived from a family's own skill phrases, plus neutral ones."""
    if lang == "cn":
        nouns = []
        for s in fam["skills_cn"]:
            for v in PAD_STRIP_VERBS_CN:
                if s.startswith(v):
                    s = s[len(v):].strip()
                    break
            nouns.append(s)
        return nouns + NEUTRAL_TECH_CN
    return list(fam["skills_en"]) + NEUTRAL_TECH_EN


def make_pad_pool(lang: str, fam: dict, rng: random.Random) -> list[str]:
    """Build a large, varied pool of realistic skill/keyword lines (no identical fillers)."""
    if lang == "cn":
        base = PAD_CN + list(fam["skills_cn"])
        tech = _family_tech(fam, "cn")
        combos = [f"{v}{t}，{c}" for t in tech for v in PAD_VERBS_CN for c in PAD_CTX_CN]
    else:
        base = PAD_EN + list(fam["skills_en"])
        tech = _family_tech(fam, "en")
        combos = [f"{v} {t}, {c}" for t in tech for v in PAD_VERBS_EN for c in PAD_CTX_EN]
    base = base[:]
    rng.shuffle(base)
    rng.shuffle(combos)
    return base + combos


def skills_lines(lang: str, arch: dict, fam: dict, rng: random.Random) -> list[str]:
    pool = list(dict.fromkeys(list(arch["expected"]) + list(fam["skills_" + lang])))
    rng.shuffle(pool)
    sep = "、" if lang == "cn" else ", "
    labels = ["核心技能", "熟练使用", "了解与实践"] if lang == "cn" else ["Core skills", "Proficient in", "Familiar with"]
    n = len(pool)
    third = max(1, n // 3)
    groups = [pool[:third], pool[third:2 * third], pool[2 * third:]]
    lines = []
    for label, grp in zip(labels, groups):
        if grp:
            lines.append(f"{label}：{sep.join(grp)}" if lang == "cn" else f"{label}: {sep.join(grp)}")
    return lines


def add_timeline(arch: dict, ctx: dict, rng: random.Random) -> None:
    level = arch["level"]
    bc, be = arch["title_base_cn"], arch["title_base_en"]
    comps = safe_sample(BIG_TECH + MID, 2, rng)

    def mkjob(start, end, current, prefix, comp, k, gap_after=False):
        if prefix == "senior":
            tc, te = "高级" + bc, "Senior " + be
        elif prefix == "junior":
            tc, te = "初级" + bc, "Junior " + be
        elif prefix == "intern":
            tc, te = bc + "实习生", "Intern " + be
        else:
            tc, te = bc, be
        pc = f"{start} - 至今" if current else f"{start} - {end}"
        pe = f"{start} - Present" if current else f"{start} - {end}"
        return {
            "period_cn": pc, "period_en": pe,
            "company_cn": comp[0], "company_en": comp[1],
            "title_cn": tc, "title_en": te, "k": k, "gap_after": gap_after,
        }

    jobs = []
    dl = "bachelor"
    if level == "senior":
        gy = rng.choice([2014, 2015, 2016, 2017])
        ctx["edu"] = f"{gy - 4}.09 - {gy}.06"
        dl = rng.choice(["bachelor", "master"])
        jobs.append(mkjob(f"{gy + 3}.07", "", True, "senior", comps[0], 5))
        jobs.append(mkjob(f"{gy}.07", f"{gy + 3}.06", False, "base", comps[1], 4))
    elif level == "mid":
        gy = rng.choice([2018, 2019, 2020, 2021])
        ctx["edu"] = f"{gy - 4}.09 - {gy}.06"
        dl = rng.choice(["bachelor", "master"])
        jobs.append(mkjob(f"{gy + 2}.07", "", True, "base", comps[0], 4))
        jobs.append(mkjob(f"{gy}.07", f"{gy + 2}.06", False, "junior", comps[1], 3))
    elif level == "junior":
        ctx["edu"] = "2019.09 - 2023.06"
        jobs.append(mkjob("2024.03", "", True, "base", comps[0], 4))
        jobs.append(mkjob("2023.06", "2023.12", False, "intern", comps[1], 3))
    elif level == "new_grad":
        ctx["edu"] = "2021.09 - 2025.06"
        jobs.append(mkjob("2024.06", "2024.12", False, "intern", comps[0], 3))
        jobs.append(mkjob("2023.07", "2023.09", False, "intern", comps[1], 3))
    elif level == "gap":
        gy = rng.choice([2017, 2018])
        ctx["edu"] = f"{gy - 4}.09 - {gy}.06"
        gs, ge = f"{gy + 3}.05", f"{gy + 4}.04"
        jobs.append(mkjob(ge, "", True, "base", comps[0], 3, gap_after=True))
        jobs.append(mkjob(f"{gy}.07", gs, False, "base", comps[1], 3))
        ctx["gap_note_cn"] = f"职业空窗期：{gs} - {ge}（因个人原因暂别职场，期间自学并参与开源项目）"
        ctx["gap_note_en"] = f"Career gap: {gs} - {ge} (break for personal reasons; self-studied and contributed to open source)"

    ctx["degree_cn"] = "本科" if dl == "bachelor" else "硕士"
    ctx["degree_en"] = "B.S." if dl == "bachelor" else "M.S."
    ctx["jobs"] = jobs


def build_ctx(arch: dict, has_github: bool, rng: random.Random) -> dict:
    sur = rng.choice(SURNAMES)
    if rng.random() < 0.55:
        giv, gender = rng.choice(GIVEN_M), ("男", "Male")
    else:
        giv, gender = rng.choice(GIVEN_F), ("女", "Female")
    name_cn = sur[0] + giv[0]
    name_en = f"{sur[1]} {giv[1]}"
    key = (sur[1] + giv[1]).lower().replace(" ", "")
    school = rng.choice(SCHOOLS)
    major = rng.choice(MAJORS)
    city = rng.choice(CITIES)
    phone = "1" + rng.choice("35789") + "".join(rng.choice("0123456789") for _ in range(9))
    domain = rng.choice(["gmail.com", "163.com", "qq.com", "outlook.com", "foxmail.com"])
    email = f"{key}{rng.randint(1, 9999)}@{domain}"
    github = f"https://github.com/{key}{rng.randint(1, 999)}"
    nums = {
        "pct": rng.randint(20, 60),
        "pct2": rng.randint(15, 55),
        "p99a": rng.choice([900, 1200, 1500, 1800, 2200, 2600]),
        "p99b": rng.choice([180, 220, 260, 320, 380, 420]),
        "qps": rng.choice([3000, 5000, 8000, 12000, 20000, 30000]),
        "daily": rng.randint(50, 900),
        "cnt": rng.choice([100, 300, 500, 1000, 3000]),
        "gb": rng.choice([2, 4, 8, 16]),
    }
    ctx = {
        "name_cn": name_cn, "name_en": name_en,
        "gender_cn": gender[0], "gender_en": gender[1],
        "school_cn": school[0], "school_en": school[1],
        "major_cn": major[0], "major_en": major[1],
        "city_cn": city[0], "city_en": city[1],
        "phone": phone, "email": email, "github": github, "has_github": has_github,
        "nums": nums,
        "courses_cn": "、".join(safe_sample(COURSES_CN, 6, rng)),
        "courses_en": ", ".join(safe_sample(COURSES_EN, 6, rng)),
    }
    add_timeline(arch, ctx, rng)
    if rng.random() < 0.6:
        gpa = f"3.{rng.randint(3, 9)}"
        rank = rng.choice([5, 10, 15, 20, 30])
        ctx["edu_extra_cn"] = f"GPA {gpa}/4.0，专业排名前 {rank}%；曾获校级奖学金"
        ctx["edu_extra_en"] = f"GPA {gpa}/4.0, top {rank}% in major; merit scholarship"
    return ctx


def build_resume(lang: str, arch: dict, fam: dict, ctx: dict, rng: random.Random) -> list[str]:
    nums = ctx["nums"]
    cn = lang == "cn"
    lines = [ctx["name_cn"] if cn else ctx["name_en"]]

    if cn:
        lines.append(f"性别：{ctx['gender_cn']}    求职意向：{arch['role_cn']}    期望城市：{ctx['city_cn']}")
        contact = f"电话：{ctx['phone']}    邮箱：{ctx['email']}"
        if ctx["has_github"]:
            contact += f"    GitHub：{ctx['github']}"
        lines.append(contact)
        lines.append("")
        lines.append("教育背景")
        lines.append(f"{ctx['edu']}    {ctx['school_cn']}    {ctx['major_cn']}（{ctx['degree_cn']}）")
        lines.append(f"主修课程：{ctx['courses_cn']}")
        if ctx.get("edu_extra_cn"):
            lines.append(ctx["edu_extra_cn"])
    else:
        lines.append(f"Gender: {ctx['gender_en']}    Objective: {arch['role_en']}    Location: {ctx['city_en']}")
        contact = f"Phone: {ctx['phone']}    Email: {ctx['email']}"
        if ctx["has_github"]:
            contact += f"    GitHub: {ctx['github']}"
        lines.append(contact)
        lines.append("")
        lines.append("Education")
        lines.append(f"{ctx['edu']}    {ctx['school_en']}    {ctx['major_en']} ({ctx['degree_en']})")
        lines.append(f"Courses: {ctx['courses_en']}")
        if ctx.get("edu_extra_en"):
            lines.append(ctx["edu_extra_en"])

    lines.append("")
    lines.append("个人简介" if cn else "Summary")
    lines.append(arch["summary_cn"] if cn else arch["summary_en"])

    lines.append("")
    if arch["exp_heading"] == "intern":
        lines.append("实习经历" if cn else "Internship Experience")
    else:
        lines.append("工作经历" if cn else "Work Experience")

    exp_pool = fam["exp_cn"] if cn else fam["exp_en"]
    jobs = ctx["jobs"]
    total = sum(j["k"] for j in jobs)
    picked = safe_sample(exp_pool, total, rng)
    pos = 0
    for j in jobs:
        comp = j["company_cn"] if cn else j["company_en"]
        title = j["title_cn"] if cn else j["title_en"]
        period = j["period_cn"] if cn else j["period_en"]
        lines.append(f"{period}    {comp}    {title}")
        for b in picked[pos:pos + j["k"]]:
            lines.append(deco(b, nums, lang, rng, 0.7))
        pos += j["k"]
        if j.get("gap_after"):
            lines.append(ctx["gap_note_cn"] if cn else ctx["gap_note_en"])

    lines.append("")
    lines.append("项目经历" if cn else "Projects")
    for p in safe_sample(fam["projects"], min(3, len(fam["projects"])), rng):
        if cn:
            lines.append(f"{p['name_cn']}（{p['stack_cn']}）")
            for b in safe_sample(p["bul_cn"], len(p["bul_cn"]), rng):
                lines.append(deco(b, nums, "cn", rng, 0.5))
        else:
            lines.append(f"{p['name_en']} ({p['stack_en']})")
            for b in safe_sample(p["bul_en"], len(p["bul_en"]), rng):
                lines.append(deco(b, nums, "en", rng, 0.5))

    lines.append("")
    lines.append("工作亮点" if cn else "Highlights")
    kw_pool = _family_tech(fam, lang)
    kws = safe_sample(kw_pool, 5, rng)
    tmpls = safe_sample(HIGHLIGHT_CN if cn else HIGHLIGHT_EN, 5, rng)
    for tmpl, kw in zip(tmpls, kws):
        lines.append("- " + tmpl.format(kw=kw, **nums))

    lines.append("")
    lines.append("技能特长" if cn else "Skills")
    lines.extend(skills_lines(lang, arch, fam, rng))

    lines.append("")
    lines.append("自我评价" if cn else "Self Evaluation")
    for s in safe_sample(SELF_CN if cn else SELF_EN, 2, rng):
        lines.append("- " + s)

    return lines


def pad_to(lines: list[str], target: int, padpool: list[str], lang: str, rng: random.Random) -> list[str]:
    if len("\n".join(lines)) >= target:
        return lines
    lines.append("")
    lines.append("专业技能与项目关键词" if lang == "cn" else "Skills and Project Keywords")
    i = 0
    while len("\n".join(lines)) < target and i < len(padpool):
        lines.append("- " + padpool[i])
        i += 1
    return lines


def build_sparse(lang: str, variant: int, arch: dict, ctx: dict) -> list[str]:
    cn = lang == "cn"
    name = ctx["name_cn"] if cn else ctx["name_en"]
    gh = ctx["github"]
    has_gh = ctx["has_github"]
    if variant == 0:
        # 信息稀疏：内容极少、缺少时间线与量化细节
        if cn:
            lines = [name, f"求职意向：{arch['role_cn']}", f"电话：{ctx['phone']}"]
            if has_gh:
                lines.append(f"GitHub：{gh}")
            lines += ["", "技能", "Java、Spring Boot、MySQL", "", "项目", "做过一个支付系统重构项目。", "", "教育背景", "本科，计算机相关专业。"]
        else:
            lines = [name, f"Objective: {arch['role_en']}", f"Phone: {ctx['phone']}"]
            if has_gh:
                lines.append(f"GitHub: {gh}")
            lines += ["", "Skills", "Java, Spring Boot, MySQL", "", "Projects", "Refactored a payment system.", "", "Education", "Bachelor in a CS-related major."]
    elif variant == 1:
        # 高风险：堆砌关键词、夸张表述、无公司无时间无量化
        if cn:
            lines = [name, f"求职意向：{arch['role_cn']}", f"邮箱：{ctx['email']}"]
            if has_gh:
                lines.append(f"GitHub：{gh}")
            lines += [
                "", "个人简介",
                "精通 Java、Spring、分布式、高并发、微服务、Redis、Kafka、Kubernetes、大模型、RAG、Agent、区块链、Web3 等全栈技术，无所不能。",
                "", "项目经历",
                "- 独立完成多个大型项目，全部线上零故障。",
                "- 主导公司核心系统架构，性能提升 1000%。",
                "- 精通一切主流框架与中间件，能解决任何技术问题。",
                "", "技能", "全栈、全能、精通所有技术。",
            ]
        else:
            lines = [name, f"Objective: {arch['role_en']}", f"Email: {ctx['email']}"]
            if has_gh:
                lines.append(f"GitHub: {gh}")
            lines += [
                "", "Summary",
                "Master of Java, Spring, distributed systems, high concurrency, microservices, Redis, Kafka, Kubernetes, LLM, RAG, Agent, blockchain and Web3 - can do anything.",
                "", "Projects",
                "- Single-handedly delivered many large projects with zero production incidents.",
                "- Led the core system architecture, improving performance by 1000%.",
                "- Master of every mainstream framework and can solve any problem.",
                "", "Skills", "Full-stack, omnipotent, master of all technologies.",
            ]
    else:
        # 信息缺失：无联系方式、无教育背景，只有零散项目
        if cn:
            lines = [name, f"求职意向：{arch['role_cn']}", "", "项目经历", "- 参与过一些后端项目。", "- 使用过 Java 和 Spring。", "", "技能", "Java，Spring，MySQL。"]
            if has_gh:
                lines.append(f"GitHub：{gh}")
        else:
            lines = [name, f"Objective: {arch['role_en']}", "", "Projects", "- Worked on some backend projects.", "- Used Java and Spring.", "", "Skills", "Java, Spring, MySQL."]
            if has_gh:
                lines.append(f"GitHub: {gh}")
    return lines


def target_for(arch: dict, rng: random.Random) -> int:
    level = arch["level"]
    if level == "new_grad":
        return rng.randint(2000, 2200)
    if level == "gap":
        return rng.randint(2000, 2180)
    return rng.randint(2000, 2350)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    rng = random.Random(seed)

    plan = [arch for arch in ARCHETYPES for _ in range(arch["count"])]
    total = len(plan)

    file_types = ["pdf"] * (total // 2) + ["txt"] * (total - total // 2)
    rng.shuffle(file_types)
    gh_count = round(total * 0.7)
    githubs = [True] * gh_count + [False] * (total - gh_count)
    rng.shuffle(githubs)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    manifest = []
    for i, arch in enumerate(plan):
        has_github = githubs[i]
        ftype = file_types[i]
        ctx = build_ctx(arch, has_github, rng)

        if arch["family"] == "sparse":
            variant = i % 3
            cn_text = "\n".join(build_sparse("cn", variant, arch, ctx))
            content = "\n".join(build_sparse("en", variant, arch, ctx)) if ftype == "pdf" else cn_text
        else:
            fam = FAMILIES[arch["family"]]
            target = target_for(arch, rng)
            cn_lines = pad_to(build_resume("cn", arch, fam, ctx, rng), target, make_pad_pool("cn", fam, rng), "cn", rng)
            cn_text = "\n".join(cn_lines)
            if ftype == "pdf":
                en_lines = pad_to(build_resume("en", arch, fam, ctx, rng), target, make_pad_pool("en", fam, rng), "en", rng)
                content = "\n".join(en_lines)
            else:
                content = cn_text

        rid = f"{arch['key']}_{i + 1:03d}"
        fname = f"{rid}.{ftype}"
        fpath = OUT / fname
        if ftype == "pdf":
            write_simple_pdf(fpath, content)
        else:
            fpath.write_text(content, encoding="utf-8")

        manifest.append({
            "id": rid,
            "name": ctx["name_cn"],
            "role": arch["role_cn"],
            "fileType": ftype,
            "path": str(fpath.relative_to(ROOT)).replace("\\", "/"),
            "hasGithub": has_github,
            "textLength": len(cn_text),
            "expectedSkills": list(arch["expected"]),
        })

    manifest_path = OUT / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    pdf_n = sum(1 for m in manifest if m["fileType"] == "pdf")
    txt_n = sum(1 for m in manifest if m["fileType"] == "txt")
    gh_n = sum(1 for m in manifest if m["hasGithub"])
    lengths = [m["textLength"] for m in manifest]
    avg = sum(lengths) / len(lengths)

    print(f"[ok] generated {total} resumes -> {OUT}")
    print(f"  pdf: {pdf_n}  txt: {txt_n}")
    print(f"  with github: {gh_n} ({round(gh_n * 100 / total)}%)")
    print(f"  textLength avg: {avg:.1f}  min: {min(lengths)}  max: {max(lengths)}")
    print(f"  manifest: {manifest_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
