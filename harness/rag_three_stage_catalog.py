#!/usr/bin/env python3
"""Build the frozen corpora and labels for the three-stage RAG benchmark.

The catalog is deterministic.  It deliberately contains adjacent job families
and repeated technology names so a system cannot get a high score by matching
one obvious keyword.  Generated artifacts are checked in under
``testdata/rag_three_stage`` and can be regenerated before an ECS run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "testdata" / "rag_three_stage"


# family, Chinese title, category, group, core skills, secondary tools,
# distinguishing production evidence, semantic paraphrase used in queries.
FAMILIES: list[tuple[str, str, str, str, list[str], list[str], str, str]] = [
    ("java_backend", "Java 后端工程师", "BACKEND", "backend", ["Java 17", "Spring Boot", "MySQL", "Redis"], ["Kafka", "JVM", "MyBatis", "Docker"], "交易链路、幂等状态机、慢查询与 JVM 故障治理", "企业服务端与高并发交易系统"),
    ("java_agent", "Java AI Agent 工程师", "AI_ENGINEERING", "agent", ["Java 21", "Spring AI", "RAG", "Agent Runtime"], ["Milvus", "BM25", "DeepSeek", "LangGraph"], "工具调用、预算控制、检查点恢复与检索评测", "大模型智能体运行时和知识检索工程"),
    ("go_backend", "Go 后端工程师", "BACKEND", "backend", ["Go", "Gin", "gRPC", "MySQL"], ["Redis", "Kafka", "pprof", "Kubernetes"], "低延迟服务、goroutine 泄漏与服务治理", "云原生高并发服务端"),
    ("cpp_backend", "C++ 后端工程师", "BACKEND", "backend", ["C++17", "Linux", "网络编程", "多线程"], ["gRPC", "CMake", "perf", "Redis"], "内存模型、锁竞争、网络时延与核心转储分析", "高性能基础服务"),
    ("python_backend", "Python 后端工程师", "BACKEND", "backend", ["Python", "FastAPI", "PostgreSQL", "Celery"], ["Redis", "SQLAlchemy", "pytest", "Docker"], "异步接口、任务可靠投递和数据库性能治理", "数据密集型 Web 服务端"),
    ("react_frontend", "React 前端工程师", "FRONTEND", "frontend", ["React", "TypeScript", "Vite", "状态管理"], ["Next.js", "Playwright", "Web Vitals", "Node.js"], "并发渲染、首屏性能、组件体系和前端可观测", "声明式组件化 Web 应用"),
    ("vue_frontend", "Vue 前端工程师", "FRONTEND", "frontend", ["Vue 3", "TypeScript", "Pinia", "Vite"], ["Nuxt", "Vitest", "Element Plus", "pnpm"], "响应式性能、低代码表单和组件库治理", "渐进式响应式 Web 应用"),
    ("mobile_flutter", "Flutter 跨端工程师", "MOBILE", "mobile", ["Flutter", "Dart", "iOS", "Android"], ["Riverpod", "Skia", "Firebase", "CI/CD"], "启动耗时、渲染卡顿、插件桥接和双端发布", "一套代码交付移动双端"),
    ("ios", "iOS 工程师", "MOBILE", "mobile", ["Swift", "UIKit", "SwiftUI", "iOS"], ["Combine", "Instruments", "CocoaPods", "XCTest"], "启动优化、内存泄漏、包体积与审核发布", "苹果移动端原生研发"),
    ("android", "Android 工程师", "MOBILE", "mobile", ["Kotlin", "Jetpack Compose", "Android", "MVVM"], ["Coroutines", "Room", "Gradle", "LeakCanary"], "ANR、冷启动、崩溃治理与模块化构建", "安卓原生客户端研发"),
    ("data_engineer", "数据工程师", "DATA", "data", ["Spark", "Flink", "Kafka", "Hive"], ["Doris", "Iceberg", "Airflow", "DataX"], "实时数仓、数据倾斜、Exactly Once 与质量 SLA", "批流一体的数据加工平台"),
    ("analytics_engineer", "分析工程师", "DATA", "data", ["dbt", "SQL", "指标体系", "维度建模"], ["Snowflake", "BigQuery", "Looker", "Git"], "语义层、数据契约、口径治理和模型测试", "连接数仓建模与业务分析"),
    ("data_analyst", "数据分析师", "DATA", "data", ["SQL", "Python", "A/B 测试", "统计分析"], ["Tableau", "Excel", "因果推断", "用户分群"], "实验设计、指标异动归因与业务策略评估", "用数据验证产品和运营决策"),
    ("ml_engineer", "机器学习工程师", "ALGORITHM", "ml", ["PyTorch", "特征工程", "模型训练", "MLOps"], ["XGBoost", "MLflow", "ONNX", "Kubernetes"], "离线指标、线上漂移、推理延迟与训练复现", "将预测模型稳定部署到生产"),
    ("llm_rag", "大模型 RAG 工程师", "AI_ENGINEERING", "agent", ["Embedding", "向量检索", "Rerank", "RAG 评测"], ["Milvus", "Elasticsearch", "Qwen", "LangChain"], "切分消融、混合召回、忠实度和检索延迟", "让生成模型基于企业知识可靠回答"),
    ("nlp", "NLP 算法工程师", "ALGORITHM", "ml", ["Transformer", "文本分类", "信息抽取", "PyTorch"], ["BERT", "CRF", "SentencePiece", "ONNX"], "标注一致性、长尾实体、领域迁移和模型压缩", "自然语言理解与文本结构化"),
    ("computer_vision", "计算机视觉工程师", "ALGORITHM", "ml", ["OpenCV", "目标检测", "图像分割", "PyTorch"], ["YOLO", "TensorRT", "CUDA", "Albumentations"], "小目标召回、数据闭环、边缘推理和显存优化", "机器视觉感知算法"),
    ("devops", "DevOps 工程师", "INFRA", "infra", ["Kubernetes", "Docker", "Terraform", "CI/CD"], ["Jenkins", "Argo CD", "Helm", "Ansible"], "发布流水线、基础设施即代码和交付效率", "研发运维一体化自动交付"),
    ("sre", "SRE 工程师", "INFRA", "infra", ["SLO", "Prometheus", "Kubernetes", "故障应急"], ["Grafana", "OpenTelemetry", "eBPF", "Go"], "错误预算、容量规划、事故复盘和降级演练", "用可靠性工程保障线上服务"),
    ("cloud_security", "云安全工程师", "SECURITY", "security", ["云原生安全", "IAM", "Kubernetes", "威胁检测"], ["Falco", "WAF", "SIEM", "Terraform"], "最小权限、容器逃逸检测与云上事件响应", "保护公有云与容器基础设施"),
    ("appsec", "应用安全工程师", "SECURITY", "security", ["代码审计", "SDL", "Web 安全", "渗透测试"], ["SAST", "DAST", "Burp Suite", "OWASP"], "漏洞复现、供应链治理和研发安全左移", "在软件生命周期内消除应用漏洞"),
    ("qa_automation", "测试开发工程师", "QA", "quality", ["Python", "接口测试", "自动化测试", "质量平台"], ["pytest", "Playwright", "JMeter", "Allure"], "测试分层、流水线准入、稳定性与缺陷逃逸率", "用工程化手段提升软件质量"),
    ("product_ai", "AI 产品经理", "PRODUCT", "product", ["AI 产品规划", "RAG", "Agent", "评测体系"], ["PRD", "用户访谈", "A/B 测试", "Prompt"], "模型边界、答案质量、权限审计与商业转化", "把大模型能力设计成可用产品"),
    ("product_b2b", "B2B 产品经理", "PRODUCT", "product", ["企业产品", "需求分析", "权限模型", "商业化"], ["PRD", "客户访谈", "SaaS", "数据分析"], "多租户、审批流程、交付边界和续费增长", "面向企业客户设计复杂业务软件"),
    ("product_growth", "增长产品经理", "PRODUCT", "product", ["增长实验", "漏斗分析", "用户分层", "A/B 测试"], ["SQL", "埋点", "留存", "转化率"], "实验显著性、渠道质量和长期留存", "通过实验驱动用户与收入增长"),
    ("ux", "用户体验设计师", "DESIGN", "design", ["交互设计", "用户研究", "设计系统", "Figma"], ["可用性测试", "原型", "无障碍", "数据分析"], "任务成功率、复杂流程、组件规范和研究闭环", "从用户研究到交互方案落地"),
    ("dba", "数据库工程师", "INFRA", "infra", ["MySQL", "PostgreSQL", "高可用", "性能优化"], ["备份恢复", "分库分表", "ProxySQL", "Prometheus"], "执行计划、复制延迟、容灾演练和数据恢复", "保障关系数据库稳定高效"),
    ("embedded", "嵌入式软件工程师", "EMBEDDED", "embedded", ["C", "RTOS", "驱动开发", "ARM"], ["FreeRTOS", "SPI", "I2C", "JTAG"], "中断时延、功耗、硬件联调和现场故障定位", "贴近硬件的实时软件研发"),
    ("game_server", "游戏服务端工程师", "GAME", "backend", ["Go", "C++", "实时同步", "分布式"], ["Redis", "UDP", "帧同步", "压测"], "房间调度、状态一致性、反作弊与延迟抖动", "多人在线游戏后端"),
    ("solution_architect", "解决方案架构师", "ARCHITECT", "architecture", ["架构设计", "云计算", "客户方案", "技术售前"], ["Kubernetes", "微服务", "成本估算", "POC"], "需求澄清、方案取舍、迁移路径和投标交付", "把客户约束转化为可落地技术方案"),
]

LEVELS = {
    "junior": ("初级", "1-3 年", "在指导下完成模块交付，能定位常见问题并补齐自动化测试", "承担一个清晰模块，说明实现细节和一次排障"),
    "senior": ("高级", "4-7 年", "独立负责核心链路，做过性能、稳定性或质量优化并有量化结果", "主导关键模块，给出基线、方案权衡和上线结果"),
    "lead": ("负责人", "8 年以上", "负责架构演进、团队协作和生产目标，对成本、质量、时延做系统取舍", "带领 4-8 人，定义指标并推动跨团队落地"),
}


def jd_description(title: str, level: str, skills: list[str], tools: list[str],
                   evidence: str, paraphrase: str, style: str) -> str:
    level_zh, years, ownership, proof = LEVELS[level]
    sections = [
        ("岗位职责", [
            f"负责{paraphrase}相关系统的设计、开发、测试与生产运行，工作范围与个人边界必须可说明",
            f"{ownership}；围绕{evidence}建立可观测指标、告警、回归与复盘机制",
            "与产品、测试和上下游团队协作，把模糊需求拆成可验收交付项，保留技术决策记录",
            "参与线上值班和容量评审，故障后给出根因、修复、预防动作及量化验证",
            f"对{'、'.join(skills[:2])}相关核心模块持续治理，建立性能基线、错误预算和发布检查项；不能只完成一次性交付",
            "针对业务峰谷、异常流量和依赖降级设计演练方案，明确恢复目标、数据一致性边界与人工兜底路径",
        ]),
        ("任职要求", [
            f"具有{years}相关经验，核心技能包括{'、'.join(skills)}",
            "能解释关键原理、适用边界和失败模式；至少有一个生产项目证明，不接受仅罗列名词",
            "有工程质量意识，包括单元测试、代码评审、版本控制、灰度发布与监控告警",
            f"能使用{'、'.join(tools[:2])}完成定位或交付，并说清输入规模、关键参数、资源消耗和结果验证方法",
            f"面对共享技术栈相近的岗位，能够说明本岗位的核心产出是{evidence}，而不是泛化成参与系统开发",
        ]),
        ("加分项", [
            f"熟悉{'、'.join(tools)}，有真实规模或故障案例",
            "做过性能或质量专项，能给出优化前后基线、机器或数据规模、测量方法",
            "有技术文档、内部分享、开源贡献或跨团队标准化经验",
            "有成本治理或资源规划经历，能解释为什么某项优化在小规模有效、扩大十倍后可能失效",
        ]),
        ("生产场景", [
            "关键依赖在峰值流量下抖动时，需要说明如何观测、隔离、降级和恢复，并用数据证明没有把故障转移给下游",
            f"围绕{evidence}出现质量回退时，要求给出最小复现、基线对照、方案 A/B 取舍、灰度门槛和回滚条件",
            f"现有{'、'.join(skills[:3])}方案的数据量或并发增长十倍时，要求估算首先到达的瓶颈、容量模型、压测设计和成本上限",
            "候选人必须区分个人完成、团队共同完成和外部平台能力，给出可核验的代码、监控、评审或事故记录类型",
        ]),
        ("经验要求", [
            proof,
            "面试需讲清一次方案取舍、一次失败或回滚，以及如何证明最终结果不是偶然波动",
            "结果指标至少覆盖质量、延迟、成本或稳定性中的两项，禁止只写显著提升",
        ]),
    ]
    display_title = f"{level_zh}{title}"
    if style == "plain_labeled":
        blocks = [display_title]
        for heading, items in sections:
            blocks.append(f"{heading}：\n" + "\n".join(
                f"{index}. {item}。" for index, item in enumerate(items, start=1)))
        return "\n\n".join(blocks)
    if style == "plain_compact":
        blocks = [f"我们正在招聘{display_title}。该岗位面向{paraphrase}，强调真实生产交付而非关键词罗列。"]
        for _, items in sections:
            blocks.append("。".join(items) + "。")
        # Mirrors current MySQL examples: one textarea paragraph with inline
        # Chinese punctuation, no Markdown and no reliable line boundaries.
        return "".join(blocks)
    raise ValueError(style)


def jd_query_resume(target: str, target_skills: list[str], target_tools: list[str],
                    decoy: tuple[str, str, str, str, list[str], list[str], str, str],
                    signal_position: str) -> str:
    """Build a resume-shaped retrieval query, not a final-resume benchmark."""
    decoy_title, decoy_skills, decoy_tools, decoy_evidence = decoy[1], decoy[4], decoy[5], decoy[6]
    neutral_blocks = [
        "个人概况：候选人持续参与企业软件交付，能够完成需求评审、开发测试、灰度上线和值班复盘；简历中的团队成果和个人成果会分别说明。",
        f"早期经历一：参与{decoy_title}相关平台，接触{'、'.join(decoy_skills[:3])}，主要承担常规需求和缺陷修复，没有负责总体架构。",
        f"早期经历二：协助使用{'、'.join(decoy_tools[:3])}完善交付流程，处理过配置错误和容量告警；这段经历与{decoy_evidence}有关但不是最近两年的主责方向。",
        "通用项目：参与内部权限、审计和配置中心建设。团队共 9 人，本人负责接口契约、测试用例和发布清单，指标包含错误率、P95 延迟和恢复时间。",
        "稳定性实践：一次依赖抖动导致请求堆积，先限流和回滚，再通过日志、指标和线程状态定位；事后增加容量演练、超时预算和回归门槛。",
        "工程质量：坚持代码评审、单元测试和变更记录，能说明方案 A/B 的取舍、失败版本、灰度比例以及为什么最终数字可复现。",
        "协作经历：与产品、测试、运维和数据团队合作，把模糊需求拆成验收项；不把云平台或公共中间件的能力包装成个人开发成果。",
        "补充项目：做过报表、消息通知、批处理和后台管理模块，关注接口幂等、失败重试、数据校验和监控告警，但这些不是目标岗位的核心证据。",
        "规模说明：常规业务日请求数百万，峰值压测持续四十五分钟；所有性能数字都记录机器规格、数据规模、基线版本和错误率。",
        "教育与其他：本科计算机相关专业，参加过内部技术分享；职业时间线完整，最近求职方向以实际主责项目为准。",
    ]
    target_block = (
        "最近核心经历（目标岗位证据）：" + target
        + f" 个人主责技术包括{'、'.join(target_skills)}，使用{'、'.join(target_tools[:3])}完成生产交付。"
        + " 能给出一次失败回滚、一次容量压测和一次线上故障的原始指标。"
    )
    filler = neutral_blocks + [
        block.replace("经历", f"经历补充{cycle}-{index}")
        for cycle in range(1, 8)
        for index, block in enumerate(neutral_blocks[3:8], start=1)
    ]
    if signal_position == "early":
        ordered = [target_block, *filler]
    elif signal_position == "middle":
        midpoint = len(filler) // 2
        ordered = [*filler[:midpoint], target_block, *filler[midpoint:]]
    else:
        ordered = [*filler, target_block]
    return "\n\n".join(ordered)


def build_jds() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    for family_index, (family, title, category, group, skills, tools, evidence, paraphrase) in enumerate(FAMILIES):
        for level_index, level in enumerate(LEVELS):
            jd_id = f"exp-jd-{family}-{level}"
            level_zh, years, _, _ = LEVELS[level]
            format_cohort = "plain_labeled" if (family_index + level_index) % 2 == 0 else "plain_compact"
            docs.append({
                "jdId": jd_id,
                "family": family,
                "level": level,
                "group": group,
                "formatCohort": format_cohort,
                "title": f"{level_zh}{title}",
                "category": category,
                "description": jd_description(title, level, skills, tools, evidence, paraphrase, format_cohort),
            })

            # Rotate query style so the set contains exact, paraphrase and hard-negative cases.
            query_style = (family_index + level_index) % 3
            shared = FAMILIES[(family_index - 1) % len(FAMILIES)]
            if query_style == 0:
                target = f"候选人有{years}经验，长期使用{'、'.join(skills[:3])}。最近项目负责{evidence}，结果有完整压测和线上监控。"
                case_type = "lexical"
            elif query_style == 1:
                target = f"候选人从事{paraphrase}，没有直接写岗位名称。项目中用{'、'.join(tools[:3])}解决生产问题，并能说明取舍与失败边界。"
                case_type = "semantic_paraphrase"
            else:
                target = f"候选人有{years}经验，既接触{shared[4][0]}也使用{'、'.join(skills[:3])}，核心贡献是{evidence}；共享技术很多，但最近两年主要负责后者。"
                case_type = "hard_negative"
            signal_position = ("early", "middle", "late")[family_index % 3]
            body = jd_query_resume(target, skills, tools, shared, signal_position)

            relevance: dict[str, int] = {jd_id: 3}
            levels = list(LEVELS)
            for other_level in levels:
                other_id = f"exp-jd-{family}-{other_level}"
                if other_id != jd_id:
                    relevance[other_id] = 2 if abs(levels.index(other_level) - level_index) == 1 else 1
            adjacent = [f for f in FAMILIES if f[3] == group and f[0] != family]
            hard_negatives: list[str] = []
            for adjacent_family in adjacent[:3]:
                adjacent_id = f"exp-jd-{adjacent_family[0]}-{level}"
                relevance[adjacent_id] = 1
                hard_negatives.append(adjacent_id)
            queries.append({
                "caseId": f"jdq-{family_index + 1:02d}-{level}",
                "stage": "jd_recall",
                "caseType": case_type,
                "query": body,
                "goldId": jd_id,
                "family": family,
                "level": level,
                "formatCohort": format_cohort,
                "signalPosition": signal_position,
                "relevance": relevance,
                "hardNegativeIds": hard_negatives,
            })
    return docs, queries


def build_resume_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index, spec in enumerate(FAMILIES):
        family, title, _, _, skills, tools, evidence, paraphrase = spec
        rid = f"resume-{index + 1:02d}-{family}"
        sections = [
            {"sectionId": "summary", "title": "个人摘要", "content": f"6 年{paraphrase}经验，目标岗位为高级{title}。既做需求交付，也参与质量治理。"},
            {"sectionId": "skills", "title": "技能", "content": f"核心技能：{'、'.join(skills)}。辅助工具：{'、'.join(tools)}。能够解释技术边界，不把工具名当成果。"},
            {"sectionId": "experience", "title": "工作经历", "content": f"2021.03 至今在甲公司负责核心平台；团队 7 人，本人负责两个模块。围绕{evidence}建立指标，年度可用性达到 99.95%。日常承担需求评审、代码评审、灰度发布和值班，明确区分个人负责模块与平台团队提供的公共能力。2022 年推动统一监控口径，把无负责人告警从每周 18 条降到 3 条，并形成事故复盘模板。"},
            {"sectionId": "project_primary", "title": "核心项目", "content": f"项目 A：面向日活 80 万用户重构关键链路。使用{skills[0]}与{tools[0]}，峰值吞吐由 900 提升到 2100，P95 从 480ms 降至 170ms。基线环境为 4 台 8 核 16GB 实例，持续压测 45 分钟，观察 CPU、内存、错误率和下游连接数；不是把瞬时峰值当稳定吞吐。第一版激进缓存导致数据更新延迟，灰度阶段回滚，第二版增加版本校验与过期兜底。三轮压测后按 5%、20%、50%、100% 放量，并与旧版本对照一周，最终错误率由 0.42% 降到 0.08%。本人负责瓶颈定位、方案取舍和压测脚本，上游容量扩容由基础设施团队完成。"},
            {"sectionId": "project_incident", "title": "故障与复盘", "content": f"项目 B：一次发布后出现连接池耗尽和请求堆积，外部表现同样包含延迟升高，因此容易与项目 A 的性能优化混淆。先根据错误率和线程状态执行限流回滚，再用{tools[1]}定位到异常分支没有释放资源；恢复时间 37 分钟。修复后补充资源关闭测试、连接池水位告警、峰值容量演练与发布前检查。复盘确认监控只覆盖平均值，遗漏 P99 和等待队列，随后增加分位延迟与饱和度面板。该事故没有吞吐从 900 到 2100 的结果，相关数字只属于项目 A。"},
            {"sectionId": "education", "title": "教育背景", "content": "2014.09-2018.06 本科，计算机相关专业；参与实验室项目但未将其包装为全职经验。"},
            {"sectionId": "risk", "title": "其他说明", "content": "2019.01-2019.10 有九个月职业空窗，用于照顾家人和系统学习，时间线有明确说明。"},
        ]
        query_specs = [
            ("技术预检", f"{' '.join(skills[:3])} 项目实践 性能优化 故障排查 量化成果", ["project_primary", "project_incident"], "workflow_tech_query", "workflow_template"),
            ("项目预检", f"项目 A {skills[0]} {tools[0]} 架构", ["project_primary"], "workflow_project_query", "workflow_template"),
            ("故障追问", f"候选人用 {tools[1]} 处理过什么事故？给出定位、止损、37 分钟恢复和预防动作", ["project_incident"], "hard_negative", "copilot_question"),
            ("时间追问", f"这位{title}是否存在超过六个月的空窗，原文有没有合理解释？", ["risk"], "cross_section", "copilot_question"),
        ]
        cases.append({
            "resumeId": rid,
            "family": family,
            "benchmarkSplit": "heldout" if index % 3 == 2 else "calibration",
            "sections": sections,
            "queries": [
                {"caseId": f"rsq-{index + 1:02d}-{qi + 1}", "label": label,
                 "query": query, "goldSections": gold, "caseType": case_type,
                 "querySource": query_source,
                 "benchmarkSplit": "heldout" if index % 3 == 2 else "calibration"}
                for qi, (label, query, gold, case_type, query_source) in enumerate(query_specs)
            ],
        })
    return cases


KB_QUERY_MAP: dict[str, list[tuple[str, str]]] = {
    "AI Agent 工程师面试 Rubric（L3-L7 分级）": [
        ("只会调用模型 API 和做单轮 demo，大概属于什么级别？", "一、分级标准"),
        ("高级智能体工程师要会哪些生产保护机制？", "一、分级标准"),
        ("面试怎么检查候选人的检索质量意识？", "二、核心考察维度"),
        ("说接了 RAG 但没有任何召回指标，应如何判断？", "三、红旗信号"),
        ("专家级智能体岗位和资深级的差别是什么？", "一、分级标准"),
    ],
    "Java 后端工程师评估标准": [
        ("线程池和 JVM 能力不能只背八股，应该怎么核查？", "一、硬性要求核查"),
        ("候选人说做过缓存一致性，追问哪些取舍？", "一、硬性要求核查"),
        ("五千 QPS 的项目数字怎样判断自洽？", "二、深度信号"),
        ("所有项目都写核心开发为什么要降权？", "三、降权信号"),
        ("如何验证 Java 工程师真的治理过慢查询？", "一、硬性要求核查"),
    ],
    "前端工程师评估标准": [
        ("会用框架和理解渲染原理分别算什么深度？", "一、框架深度分层"),
        ("前端性能优化必须提供哪些量化基线？", "二、性能优化核查"),
        ("长列表优化手段和使用场景怎么验证？", "二、性能优化核查"),
        ("复杂编辑器或跨端经历是否属于加分？", "三、加分与红旗"),
        ("把脚手架初始化说成架构设计是什么信号？", "三、加分与红旗"),
    ],
    "算法与大模型工程师评估标准": [
        ("声称做过微调，需要问清哪四个要素？", "一、真实性核查"),
        ("模型准确率 95% 为什么不能直接相信？", "一、真实性核查"),
        ("推理部署能力应该核查哪些真实数字？", "二、工程落地能力"),
        ("只调用接口却写训练大模型是什么风险？", "三、大模型应用方向补充"),
        ("怎样确认 LoRA 项目不是把微调夸成预训练？", "三、大模型应用方向补充"),
    ],
    "数据工程师评估标准": [
        ("PB 级数仓的规模真实性要用哪些数字交叉验证？", "一、规模真实性"),
        ("Spark 数据倾斜经历应该追问到什么程度？", "二、技术深度核查"),
        ("Flink exactly once 的边界怎么核查？", "二、技术深度核查"),
        ("只列 Hive Spark Flink 为什么不能证明有经验？", "三、降权信号"),
        ("数仓口径治理应该有哪些落地证据？", "二、技术深度核查"),
    ],
    "项目真实性核验清单": [
        ("STAR 中如何区分参与、负责和主导？", "一、STAR 完整性检查"),
        ("性能提升 300% 没有起点数字能采信吗？", "二、量化指标可信度判定"),
        ("日活千级却写支撑亿级流量是什么问题？", "二、量化指标可信度判定"),
        ("怎么用任职时间交叉验证项目真假？", "三、交叉验证动作"),
        ("开源链接和简历声明应怎样比对？", "三、交叉验证动作"),
    ],
    "时间线风险判定标准": [
        ("两段全职工作重叠三个月属于什么风险？", "一、高风险"),
        ("没有说明的八个月空窗应该怎么处理？", "二、中风险"),
        ("实习和在校时间重叠需要扣分吗？", "三、合理情形"),
        ("时间线风险输出为什么必须引用原文和月份？", "四、输出要求"),
        ("连续三段不足半年说明什么？", "一、高风险"),
    ],
    "技术深度信号词典": [
        ("方案里讲了为了某目标牺牲另一目标，算什么信号？", "一、深度实践信号"),
        ("技能清单很长但项目没出现，应该如何处理？", "二、关键词堆砌信号"),
        ("怎么把深度表述转成验证型追问？", "三、追问转化规则"),
        ("候选人主动讲失败和回滚为什么有价值？", "一、深度实践信号"),
        ("高并发高可用却没有对象和数字是什么信号？", "二、关键词堆砌信号"),
    ],
    "面试追问题库（按风险类型）": [
        ("QPS 数字存疑时要追问机器和压测哪些信息？", "一、针对指标存疑"),
        ("如何追问候选人在团队里的个人贡献边界？", "二、针对职责边界模糊"),
        ("八个月空窗应该怎样开放式提问？", "三、针对时间线异常"),
        ("如果数据量涨十倍，怎样验证技术深度？", "四、针对技术深度验证"),
        ("为什么不能给所有候选人输出同一套通用问题？", "五、使用规则"),
    ],
    "评分与推荐一致性规则": [
        ("综合分 58 分应该给什么推荐结论？", "一、分数区间与推荐映射"),
        ("核心维度低于四十分时总分上限是多少？", "二、维度分与综合分自洽"),
        ("证据不足时能不能随手给中间分？", "二、维度分与综合分自洽"),
        ("八十八分却不推荐为什么需要额外说明？", "三、禁止事项"),
        ("报告修订后分数变化过大要做什么？", "三、禁止事项"),
    ],
    "教育背景与证书权重指引": [
        ("工作五年的候选人学历最多占多大权重？", "一、学历权重原则"),
        ("什么样的竞赛或论文能算教育背景加分？", "二、加分项判定"),
        ("证书和岗位无关时应该怎么处理？", "三、证书处理规则"),
        ("能否因为学校一般直接否定有经验的候选人？", "四、禁止事项"),
        ("应届生和资深人员的学历权重为何不同？", "一、学历权重原则"),
    ],
    "英文简历评估补充规范": [
        ("Staff Engineer 应如何对标国内职级？", "一、职级词校准"),
        ("英文简历里的 led 和 owned 一定代表负责人吗？", "二、动词包装识别"),
        ("英文数字和日期格式怎样避免解析误差？", "三、量化与格式"),
        ("简历用英文写能否直接证明口语流利？", "四、语言能力推断边界"),
        ("Principal 头衔为什么必须结合组织规模判断？", "一、职级词校准"),
    ],
}


def load_seed_kb() -> list[dict[str, Any]]:
    path = ROOT / "scripts" / "seed_knowledge_base.py"
    spec = importlib.util.spec_from_file_location("seed_knowledge_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    docs = []
    for index, doc in enumerate(module.DOCS):
        docs.append({"docId": f"kb-exp-{index + 1:02d}", **doc})
    return docs


def build_kb_queries(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for doc_index, doc in enumerate(docs):
        title = doc["title"]
        specs = KB_QUERY_MAP.get(title)
        if not specs or len(specs) != 5:
            raise ValueError(f"KB query map must contain five queries for {title}")
        for index, (query, section_hint) in enumerate(specs):
            rows.append({
                "caseId": f"kbq-{doc['docId'][-2:]}-{index + 1}",
                "stage": "knowledge_recall",
                "query": query,
                "goldDocIds": [doc["docId"]],
                "goldTitles": [title],
                "goldSectionHints": [section_hint],
                "goldSectionsByTitle": {title: [section_hint]},
                "caseType": ["lexical", "semantic_paraphrase", "hard_negative", "policy_boundary", "long_tail"][index],
                "querySource": "copilot_question",
                "benchmarkSplit": "calibration" if doc_index < 8 else "heldout",
            })
    by_title = {doc["title"]: doc["docId"] for doc in docs}
    workflow_specs = [
        ("技术评估 Java Spring Redis 标准", {
            "Java 后端工程师评估标准": ["一、硬性要求核查", "二、深度信号"],
        }),
        ("技术评估 前端 React Vue 标准", {
            "前端工程师评估标准": ["一、框架深度分层", "二、性能优化核查"],
        }),
        ("技术评估 RAG LLM Agent 标准", {
            "AI Agent 工程师面试 Rubric（L3-L7 分级）": ["一、分级标准", "二、核心考察维度"],
            "算法与大模型工程师评估标准": ["二、工程落地能力", "三、大模型应用方向补充"],
        }),
        ("技术评估 Flink 数据 标准", {
            "数据工程师评估标准": ["一、规模真实性", "二、技术深度核查"],
        }),
        ("技术评估 AI LLM 算法 标准", {
            "算法与大模型工程师评估标准": ["一、真实性核查", "三、大模型应用方向补充"],
            "AI Agent 工程师面试 Rubric（L3-L7 分级）": ["一、分级标准", "二、核心考察维度"],
        }),
        ("技术能力评估标准 评分规范", {
            "AI Agent 工程师面试 Rubric（L3-L7 分级）": ["二、核心考察维度"],
            "Java 后端工程师评估标准": ["一、硬性要求核查"],
            "前端工程师评估标准": ["一、框架深度分层"],
            "算法与大模型工程师评估标准": ["一、真实性核查"],
            "数据工程师评估标准": ["二、技术深度核查"],
        }),
        ("简历评估 评分标准 录用建议 风险判断", {
            "评分与推荐一致性规则": ["一、分数区间与推荐映射", "二、维度分与综合分自洽"],
            "时间线风险判定标准": ["一、高风险", "二、中风险"],
            "项目真实性核验清单": ["一、STAR 完整性检查", "二、量化指标可信度判定"],
        }),
    ]
    for index, (query, sections_by_title) in enumerate(workflow_specs, start=1):
        titles = list(sections_by_title)
        rows.append({
            "caseId": f"kbq-workflow-{index:02d}",
            "stage": "knowledge_recall",
            "query": query,
            "goldDocIds": [by_title[title] for title in titles],
            "goldTitles": titles,
            "goldSectionHints": [],
            "goldSectionsByTitle": sections_by_title,
            "caseType": "workflow_template",
            "querySource": "workflow_template",
            "benchmarkSplit": "operational",
        })
    return rows


def validate(jds: list[dict[str, Any]], jd_queries: list[dict[str, Any]],
             resumes: list[dict[str, Any]], kb_docs: list[dict[str, Any]],
             kb_queries: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(jds) == 120 and len({d["jdId"] for d in jds}) == 120
    assert len(jd_queries) == 120 and len({q["caseId"] for q in jd_queries}) == 120
    assert len(resumes) == 30
    assert sum(len(r["queries"]) for r in resumes) == 120
    assert len(kb_docs) == 12 and len(kb_queries) == 67
    for q in jd_queries:
        assert q["goldId"] in {d["jdId"] for d in jds}
        assert q["relevance"].get(q["goldId"]) == 3
    for q in kb_queries:
        doc = next(d for d in kb_docs if d["docId"] == q["goldDocIds"][0])
        assert all(hint in doc["content"] for hint in q["goldSectionHints"])
    return {
        "jdDocuments": len(jds),
        "jdQueries": len(jd_queries),
        "resumeDocuments": len(resumes),
        "resumeQueries": sum(len(r["queries"]) for r in resumes),
        "knowledgeDocuments": len(kb_docs),
        "knowledgeQueries": len(kb_queries),
        "totalQueries": len(jd_queries) + sum(len(r["queries"]) for r in resumes) + len(kb_queries),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_frozen_real_jds(out: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load, but never regenerate, the public-dataset JD benchmark rows.

    The old ``build_jds`` helper remains only so historical benchmark fixtures
    can still be inspected.  It must not overwrite the real JD catalog used by
    the three-stage experiment.
    """
    catalog_path = out / "jd_catalog.json"
    query_path = out / "jd_queries.json"
    if not catalog_path.exists() or not query_path.exists():
        raise SystemExit(
            "real JD corpus missing; run scripts/build_real_jd_rag_corpus.py first"
        )
    jds = json.loads(catalog_path.read_text(encoding="utf-8"))
    queries = json.loads(query_path.read_text(encoding="utf-8"))
    if len(jds) != 120 or any(
        not str(row.get("jdId", "")).startswith("exp-real-jd-")
        or not (row.get("source") or {}).get("dataset")
        or (row.get("source") or {}).get("license") != "apache-2.0"
        for row in jds
    ):
        raise SystemExit(
            "refusing to overwrite jd_catalog.json: expected 120 sourced exp-real-jd-* rows"
        )
    return jds, queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    jds, jd_queries = load_frozen_real_jds(args.out)
    resumes = build_resume_cases()
    kb_docs = load_seed_kb()
    kb_queries = build_kb_queries(kb_docs)
    manifest = validate(jds, jd_queries, resumes, kb_docs, kb_queries)
    manifest.update({
        "schemaVersion": 1,
        "frozen": True,
        "stages": ["jd_recall", "resume_evidence", "knowledge_recall"],
        "note": (
            "JD documents are frozen Apache-2.0 public dataset rows; JD retrieval queries and "
            "the resume/KB labels are synthetic and production-shaped. Never use full-resume "
            "final scores as RAG relevance labels."
        ),
    })

    write_json(args.out / "jd_catalog.json", jds)
    write_json(args.out / "jd_queries.json", jd_queries)
    write_json(args.out / "resume_evidence_cases.json", resumes)
    write_json(args.out / "knowledge_documents.json", kb_docs)
    write_json(args.out / "knowledge_queries.json", kb_queries)
    write_json(args.out / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
