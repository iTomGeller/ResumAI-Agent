#!/usr/bin/env python3
"""Seed the knowledge base with 12 high-quality evaluation-standard documents.

Each document is 800-1500 chars of structured Chinese content (markdown
headings + numbered criteria — exactly the shape the structure-aware chunker
splits best). Idempotent: existing documents with the same title are skipped
unless --force re-uploads them.

Usage (on ECS, after the stack is up):
  python3 scripts/seed_knowledge_base.py --base http://127.0.0.1
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DOCS: list[dict] = [
    {
        "title": "AI Agent 工程师面试 Rubric（L3-L7 分级）",
        "docType": "interview_rubric",
        "tags": "ai_agent,rubric,leveling",
        "content": """# AI Agent 工程师面试 Rubric

## 一、分级标准
1. L3（初级）：能调用 LLM API 完成单轮任务；理解 prompt 基本结构；有 demo 级 RAG 或工具调用经验。
2. L4（中级）：独立实现多轮 agent 循环（规划-工具-观察）；理解上下文窗口管理与 token 预算；做过检索链路（分块、向量化、召回）并能说出参数选择理由。
3. L5（高级）：设计过完整 agent harness：预算强制、死循环防护、失败降级、可观测性；能讲清多 agent 协作的状态共享与冲突处理；有生产化部署与线上问题排查经历。
4. L6（资深）：主导过 agent 系统架构演进；有评测体系建设经验（golden set、离线回归、在线采样）；能量化说明优化收益（成本/时延/质量三角权衡）。
5. L7（专家）：定义团队级 agent 技术路线；有策略学习/自动优化环路落地；对业界框架取舍有一手实验证据。

## 二、核心考察维度
1. 工程化深度：prompt 版本管理、结构化输出保障（function calling / json mode / schema 校验分层）、重试与熔断。
2. 检索质量意识：是否做过 recall/MRR 评测；分块与 overlap 是实验定参还是拍脑袋。
3. 成本意识：能否说出单次任务的 token 成本构成与缓存策略（前缀缓存、语义缓存）。
4. 评测能力：有无 held-out 验证意识；是否理解 reward hacking 风险。

## 三、红旗信号
1. 只会串 API、说不清失败模式处理。
2. 声称"接入了 RAG"但说不出召回率或任何量化指标。
3. 把 LangChain/LangGraph 等框架名当能力本身，追问原理即卡壳。""",
    },
    {
        "title": "Java 后端工程师评估标准",
        "docType": "tech_guide",
        "tags": "java,backend,rubric",
        "content": """# Java 后端工程师评估标准

## 一、硬性要求核查
1. 并发与 JVM：要求给出真实案例——线程池参数如何定、什么场景出现过 OOM/FGC、如何定位（工具与指标）。只背八股不加分。
2. 数据库：索引设计依据（区分度、最左前缀）、慢查询治理的量化前后对比、事务隔离级别选择的业务理由。
3. 缓存与消息：缓存一致性方案的取舍（旁路 vs 双写 vs 订阅 binlog）；消息堆积处理经历（堆积量、消费速率、解决手段）。
4. 分布式：幂等设计的具体实现（唯一键/状态机/去重表）；分布式锁的坑（超时续期、误删）。

## 二、深度信号
1. 性能数字自洽：QPS、RT、机器规格三者能相互印证；说不出机器数的"5000 QPS"按待核实处理。
2. 有明确的个人边界："我负责其中的 X 模块"优于"我们做了整个系统"。
3. 故障复盘经历：能讲清根因链路而不是"重启解决"。

## 三、降权信号
1. 技能栏罗列 20+ 技术但项目里只出现 3 种。
2. 所有项目都是"核心开发"但说不出任何技术决策理由。
3. 微服务/中台等词汇堆砌，无拆分依据与团队规模佐证。""",
    },
    {
        "title": "前端工程师评估标准",
        "docType": "tech_guide",
        "tags": "frontend,rubric",
        "content": """# 前端工程师评估标准

## 一、框架深度分层
1. API 级：会用 React/Vue 完成业务，说不清响应式原理或渲染机制——按初中级评估。
2. 原理级：能讲 diff 策略、依赖收集、并发渲染调度，且有据此解决实际问题的案例——中高级信号。
3. 工程级：主导过构建优化（产物体积、构建耗时前后对比）、微前端/monorepo 落地——高级信号。

## 二、性能优化核查
1. 必须有量化指标：LCP/FCP/TTI 优化前后数字，或包体积从 X 降到 Y。
2. 手段与场景匹配：长列表虚拟化、图片懒加载、代码分割各自的适用条件。
3. 只写"性能优化 50%"不写基线与测量方法的，标记待核实。

## 三、加分与红旗
1. 加分：跨端方案（RN/Flutter/小程序）踩坑经历；可视化/编辑器等复杂交互实现；前端监控体系搭建。
2. 红旗：作品链接打不开或内容为模板项目；CSS 基础问题回避（层叠、BFC）；把脚手架初始化说成架构设计。""",
    },
    {
        "title": "算法与大模型工程师评估标准",
        "docType": "tech_guide",
        "tags": "algorithm,llm,rubric",
        "content": """# 算法与大模型工程师评估标准

## 一、真实性核查（最高优先级）
1. 训练/微调声明必须四要素齐全：数据规模、算力配置、训练时长、评测集与指标提升。缺任一项追问，缺两项以上按存疑处理。
2. 指标提升必须有 baseline 对照：无对照的"准确率 95%"无意义。
3. 竞赛名次核对赛道与队伍规模；论文核对作者位次与会议级别。

## 二、工程落地能力
1. 推理优化：量化（精度损失数字）、批处理、KV cache、投机解码——至少一项有实操。
2. 部署经历：显存估算方法、并发与吞吐的实测数字。
3. 数据工程：清洗规则如何定、去重策略、数据配比实验。

## 三、大模型应用方向补充
1. SFT/RLHF/DPO 至少能讲清一种的完整流程与踩坑。
2. 评测意识：自建评测集的构造方法、污染防范。
3. 红旗：只有 API 调用经历却写"大模型训练"；把 LoRA 微调 7B 说成"训练大模型"却答不出学习率量级。""",
    },
    {
        "title": "数据工程师评估标准",
        "docType": "tech_guide",
        "tags": "data,rubric",
        "content": """# 数据工程师评估标准

## 一、规模真实性
1. 数据量三件套：日增量、总存储、峰值吞吐——三者与集群规模互相印证。
2. "PB 级数仓"必须能说出分层存储策略与成本治理手段。
3. 任务规模：调度任务数、核心链路 SLA、值班故障频率。

## 二、技术深度核查
1. 数仓建模：维度建模落地案例——总线矩阵、缓慢变化维处理、口径治理机制。
2. 计算引擎：Spark 数据倾斜的定位与解决（具体到 key 分布分析）；Flink 状态管理与 exactly-once 的实现边界。
3. 数据质量：规则体系（完整性/一致性/及时性）与拦截机制，出过的数据事故与修复。

## 三、降权信号
1. 只列工具链（Hive/Spark/Flink/DolphinScheduler）不带任何规模数字。
2. "负责数仓建设"但说不出分层依据与主题域划分。
3. 指标口径问题零感知——数据工程师的核心痛点缺失说明经验浅。""",
    },
    {
        "title": "项目真实性核验清单",
        "docType": "policy",
        "tags": "verification,star,authenticity",
        "content": """# 项目真实性核验清单

## 一、STAR 完整性检查
1. Situation：项目背景是否具体（业务规模、团队规模、时间段）。
2. Task：个人职责边界是否清晰——"参与"与"负责"与"主导"必须区分。
3. Action：技术动作是否有决策理由（为什么选 A 不选 B）。
4. Result：结果是否量化且可归因到个人动作。

## 二、量化指标可信度判定
1. 指标必须有基线：优化"提升 300%"没有起点数字的按存疑处理。
2. 指标与手段匹配：加个缓存不太可能带来"响应时间从 2s 到 20ms"以外的十倍级全链路提升。
3. 指标与规模匹配：日活千级的系统写"支撑亿级流量"直接标记矛盾。
4. 多项目指标雷同（都是 50%、300%）是模板化编造的强信号。

## 三、交叉验证动作
1. 时间线交叉：项目时段与任职时段必须重合。
2. 技术栈交叉：项目所用技术应出现在技能清单，反之技能清单核心项应有项目支撑。
3. 声明的开源/博客链接必须实际抓取核验，内容与简历声明比对。
4. 同公司多项目并行超过 3 个时，追问精力分配。""",
    },
    {
        "title": "时间线风险判定标准",
        "docType": "policy",
        "tags": "timeline,risk",
        "content": """# 时间线风险判定标准

## 一、高风险（必须报告并生成追问）
1. 全职经历时间重叠超过 1 个月且无说明。
2. 出现未来时间（结束时间晚于当前日期且未标"至今"）。
3. 单段经历不足 6 个月且连续出现 3 次以上（高频跳槽模式）。
4. 教育时间与全职工作时间大面积重叠（非实习、非在职深造说明）。

## 二、中风险（报告提示，不否决）
1. 空窗期 6-12 个月无说明——生成开放式追问而非直接扣分。
2. 相邻两段经历首尾月份重叠 1 个月内——常见交接期，提示即可。
3. 时间只写年份不写月份——降低时间线置信度，标注精度不足。

## 三、合理情形（不判风险）
1. 实习与在校时间重叠。
2. 自由职业/顾问期与项目制工作并行且有说明。
3. 疫情期/进修期空窗有明确说明。

## 四、输出要求
1. 每条风险必须引用原文行号，给出重叠/空窗的具体月份计算。
2. 风险等级与建议动作绑定：高风险=背调核实项，中风险=面试追问项。""",
    },
    {
        "title": "技术深度信号词典",
        "docType": "tech_guide",
        "tags": "depth_signal,keyword",
        "content": """# 技术深度信号词典

## 一、深度实践信号（加分）
1. 带权衡的表述："为了 X 牺牲了 Y"、"对比过 A/B 两个方案"、"最终没有采用 Z 因为..."。
2. 带失败的表述："第一版设计有缺陷"、"上线后发现"、"回滚过一次"。
3. 带测量的表述：任何指标带测量方法与工具（压测工具、监控面板、profile 手段）。
4. 带边界的表述："这个方案只适用于"、"数据量超过 X 后需要"。

## 二、关键词堆砌信号（降权）
1. 技能栏超长清单但项目描述不含其中大多数技术。
2. 形容词密集：精通/熟练掌握超过 5 项核心技术却无对应深度证据。
3. 通用模板句："高并发高可用"、"性能优化"、"架构设计"不带任何具体对象。

## 三、追问转化规则
1. 每个深度信号生成一个验证型追问（细节能否展开）。
2. 每个堆砌信号生成一个证伪型追问（要求给出最小具体案例）。
3. 词典仅作证据参考，不得单独作为评分依据——必须结合项目上下文。""",
    },
    {
        "title": "面试追问题库（按风险类型）",
        "docType": "interview_rubric",
        "tags": "interview,question_bank",
        "content": """# 面试追问题库

## 一、针对指标存疑
1. "这个 QPS 数字是怎么测出来的？压测工具、机器规格、持续时长分别是什么？"
2. "优化前的基线数字是多少？是谁测的、在什么环境测的？"
3. "这个提升里，你个人的动作贡献了哪一部分？"

## 二、针对职责边界模糊
1. "这个项目团队几个人？你具体负责哪几个模块？"
2. "如果我找你当时的 TL 核实，他会怎么描述你的贡献？"
3. "项目里最难的技术决策是什么？是谁拍的板？"

## 三、针对时间线异常
1. "简历上 A 公司和 B 公司时间有重叠，能说明一下吗？"
2. "这段 8 个月的空窗期主要在做什么？"

## 四、针对技术深度验证
1. "你提到用了 X 技术，如果数据量再涨十倍，这个方案哪里先崩？"
2. "当时有没有考虑过替代方案？为什么没选？"
3. "这个系统出过的最严重的一次故障是什么？怎么定位的？"

## 五、使用规则
1. 每份报告的追问必须与该候选人的具体风险点绑定，禁止输出通用问题。
2. 追问附考察点与好答案信号，供面试官现场判断。""",
    },
    {
        "title": "评分与推荐一致性规则",
        "docType": "policy",
        "tags": "scoring,consistency",
        "content": """# 评分与推荐一致性规则

## 一、分数区间与推荐映射
1. 85-100：HIRE 或 INTERVIEW_RECOMMEND；给 HIRE 必须证据支持率 ≥ 0.7 且无高风险项。
2. 70-84：INTERVIEW_RECOMMEND 为主；存在未核实高风险时降为 NEED_MANUAL_REVIEW。
3. 55-69：NEED_MANUAL_REVIEW；明确列出复核项清单。
4. 55 以下：NOT_RECOMMEND；必须列出不推荐的具体依据（缺口/风险），禁止只写"整体较弱"。

## 二、维度分与综合分自洽
1. 综合分与维度分加权结果偏差超过 15 分时，必须在报告中说明偏差原因（如一票否决项）。
2. 任一核心维度低于 40 分时，综合分不得高于 75。
3. 证据不足以支撑打分时输出 null 并说明缺什么材料，禁止给中间值充数。

## 三、禁止事项
1. 禁止分数与结论矛盾（如 88 分 + 不推荐、52 分 + 建议录用）无说明。
2. 禁止用"建议面试确认"回避所有判断——报告必须有明确倾向。
3. 修订版本（revision）之间分数波动超过 10 分时必须说明新证据。""",
    },
    {
        "title": "教育背景与证书权重指引",
        "docType": "policy",
        "tags": "education,weighting",
        "content": """# 教育背景与证书权重指引

## 一、学历权重原则
1. 工作 3 年以内：学历与在校成果占评估权重上限 25%。
2. 工作 3-8 年：权重降至 10% 以内，以项目与工程能力为主。
3. 工作 8 年以上：学历仅作背景信息，不参与扣分。

## 二、加分项判定
1. 计算机相关专业的算法/系统课程实践（有作品）优于单纯绩点。
2. 在校竞赛：ACM/ICPC 区域赛及以上、Kaggle 前 5% 才计入加分。
3. 专升本/自考不扣分——按同等经验年限评估工程能力。

## 三、证书处理规则
1. 云厂商认证（ACP/ACE 等）：初中级岗位小幅加分，高级岗位不加分。
2. PMP/软考：技术岗不加分，不扣分。
3. 与岗位无关的证书堆砌（超过 5 个）提示"重心分散"待面试确认。

## 四、禁止事项
1. 禁止因学校层级直接给出否定性结论——只影响权重内的分项分。
2. 海外学历注意学制差异，一年制硕士不做负面推断，看课程与产出。""",
    },
    {
        "title": "英文简历评估补充规范",
        "docType": "policy",
        "tags": "english,resume",
        "content": """# 英文简历评估补充规范

## 一、职级词校准
1. Senior/Staff/Principal 不直接映射国内职级：50 人创业公司的 Staff 可能相当于大厂高级；必须结合公司规模与团队规模校准。
2. Lead 出现时核对 report line：带人数的 "led a team of N" 才按管理经验计。

## 二、动词包装识别
1. spearheaded/orchestrated/championed 等强动词不作为深度证据，只认其后的量化结果与技术细节。
2. "involved in"/"participated in" 表述的项目按参与者而非负责人评估。

## 三、量化与格式
1. 英文简历惯例是每条 bullet 带数字——缺失量化反而是负信号（比中文简历更严格）。
2. GPA 满分制校准：4.0 制、5.0 制、百分制换算后再比较，并在报告中注明原始制式。
3. 学制差异：一年制硕士、三年制本科按学分与课程内容评估，不做制度性扣分。

## 四、语言能力推断边界
1. 英文简历本身不能证明口语能力——语言要求高的岗位仍需标注"口语待面试验证"。
2. 中英混排简历检查关键信息一致性，不一致处标记待核实。""",
    },
]


def http(method: str, url: str, body: dict | None = None, timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1")
    parser.add_argument("--force", action="store_true",
                        help="re-upload even when a doc with the same title exists")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    try:
        listing = http("GET", f"{base}/api/rag/knowledge-base/documents")
        docs = listing.get("documents", []) if isinstance(listing, dict) else listing
        existing_titles = {str(d.get("title")) for d in docs}
    except Exception:
        existing_titles = set()

    created = skipped = failed = 0
    for doc in DOCS:
        if not args.force and doc["title"] in existing_titles:
            print(f"SKIP  {doc['title']}")
            skipped += 1
            continue
        try:
            result = http("POST", f"{base}/api/rag/knowledge-base/documents", {
                "title": doc["title"],
                "content": doc["content"],
                "docType": doc["docType"],
                "tags": doc["tags"],
            })
            document = result.get("document", result)
            print(f"OK    {doc['title']} -> {document.get('docId')} "
                  f"({document.get('chunkCount')} chunks)")
            created += 1
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"FAIL  {doc['title']}: {exc}")
            failed += 1

    print(f"\nseeded: created={created} skipped={skipped} failed={failed} "
          f"total_defined={len(DOCS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
