# 三阶段 RAG 实验设计、候选方案与数据集说明

本文基于项目中的冻结实验语料、实验代码和最终报告整理。这里的“三阶段”不是线上依次执行的三个节点，而是三套彼此独立的离线检索任务：

| 检索任务 | 输入 Query | 检索范围 | 实验目标 |
|---|---|---|---|
| JD 召回 `jd_recall` | 简历形态的长文本 | 120 份 JD | 找到与候选人经历匹配的岗位，并正确排序 |
| 简历证据召回 `resume_evidence` | 技术、项目、故障或时间线问题 | 当前一份简历 | 找到支持判断的原文 Section，避免跨候选人和跨章节污染 |
| 知识库召回 `knowledge_recall` | 技术评价、风险判断或评分问题 | 19 份知识文档 | 找到正确文档和可以引用的具体章节 |

## 1. 整体实验流程

三套 RAG 使用相同的实验框架，但分别选参数，不共享一套所谓的“万能配置”。

```text
冻结语料、Query 和 Gold 标注
    ↓
按文档隔离 calibration / held-out
    ↓
依次执行六类单因素实验
Chunking → Embedding → Tokenizer → Retrieval → Rewrite → Rerank
    ↓
每个场景筛选 48 套联合参数组合
    ↓
选择 11 套 finalist，检查参数交互和 Pareto 前沿
    ↓
最终 winner 与生产 baseline 在冻结 held-out 上比较
    ↓
执行 2,000 次 paired bootstrap，报告总体及分层置信区间
```

实验遵循以下规则：

1. `calibration` 用于比较候选、选择参数；`held-out` 不参与调参。
2. 单因素 winner 只解释当前其他参数固定时的效果，不能直接把六个 winner 机械拼成最终配置。
3. 最终选择同时考察 Recall@5、NDCG@10、MRR、Zero-hit、P95、索引体积和外部调用数。
4. 综合效用差不超过 `0.005` 时，优先选择索引更小、调用更少、实现更简单的配置。
5. Rewrite 或 Rerank 相对 `none` 的效用提升不足 `0.01` 时不上线。
6. 外部模型不可用时必须记录为 `unavailable`，不能静默替换成其他模型。

## 2. 主要候选方案

### 2.1 Chunking

主要候选包括：

- `whole_document`：整篇文档不切，作为负对照。
- `fixed_char`：固定字符窗口。
- `zh_boundary`：在换行、句号、分号、逗号等中文边界附近切分。
- 生产递归规则：复刻当时生产中的换行/行/字符窗口逻辑。
- `section` / `section_prefix`：按岗位职责、任职要求、项目经历、Markdown 标题等结构切分，并选择是否给 Chunk 添加文档标题和章节标题。
- `semantic`：根据相邻句 Embedding 距离寻找语义断点。
- `section + semantic`：先保留章节边界，再对过长章节做语义切分。
- 长度和 overlap：主要覆盖 `256/320/400/500/512` 字及多组 overlap。

当前实验没有使用 Jieba、HanLP、PKUSeg 或 THULAC 驱动 Chunk 边界。Jieba 只参与后面的 BM25 Tokenizer 实验。因此，`section_prefix` winner 只能说明它战胜了本实验现有的 Chunk 候选，不能说明它战胜了所有中文 NLP 切分方案。

### 2.2 Embedding

- `text-embedding-v3`：256、512、768、1024 维。
- `text-embedding-v4`：512、1024 维。
- 简历证据场景保留“不使用 Dense”的词法基线。

### 2.3 中文 Tokenizer / Sparse 检索

- `cjk_bigram`：英文按词，连续中文使用滑动双字。
- `unigram_plus_bigram`：中文单字和双字同时进入索引。
- `jieba`：Jieba 中文分词。
- `domain_maxmatch_alias`：对技术领域词做最长匹配，并归一化 `K8s/Kubernetes`、`大模型/LLM`、`智能体/Agent` 等别名。
- 原生产逻辑：简历的 phrase contains、知识库的 title/section/content/tags 加权 contains；这些基线不冒充标准 BM25。

### 2.4 Retrieval

- 纯词法/BM25：`lexical_only`。
- 纯向量：`dense_only`。
- Hybrid：BM25 与 Dense 融合。
- `semanticWeight`：主要测试 0.3、0.5、0.7。
- RRF `k`：主要测试 10、30、60、100。
- Score threshold、candidateLimit、Dense candidate multiplier 的多组组合。
- 简历场景额外要求 scoped retrieval，只能在当前候选人的简历范围内召回。

### 2.5 Query Rewrite

- `none`：原 Query 直接检索。
- 确定性改写：结构提取和技术别名归一化，不生成原文不存在的新事实。
- DeepSeek JSON Rewrite：使用 LLM 生成检索 Query。

### 2.6 Rerank

- `none`。
- 项目原有的 feature/overlap-density rerank。
- Qwen3 reranker。
- DeepSeek listwise rerank。

Qwen3 和 DeepSeek 的部分实验状态为 `unavailable`。这只能说明当次外部调用不具备可用性，不能据此得出模型质量不如本地方案的结论。

## 3. JD 召回数据集

### 3.1 规模和来源

- 120 份真实公开中文 JD。
- 120 个程序生成的简历形态 Query。
- 80 个 calibration，40 个 held-out。
- 短、中、长 JD 各 40 份。
- 279 个 Gold spans。
- JD 来自 Apache-2.0 公开数据集，经过移除 XML、空白归一化、去重和岗位类别映射。

JD 文档大致如下：

```text
标题：产品开发-软件开发

工作职责：
1. 制定软件开发计划，负责架构设计、编码……
2. 负责测试工具链部署、静态代码分析……
3. 进行单元、集成和整车测试……

任职资格：
本科及以上学历；
掌握 CAN、LIN、UDS；
熟悉底层驱动移植……
```

### 3.2 Query 如何生成

JD Query 不是真实用户简历，而是程序生成的长简历形态文本：

```text
工作经历：参与需求评审、开发测试、灰度上线和值班复盘……

项目经历：处理过依赖抖动、请求堆积和数据校验问题……

最近两年承担目标方向的核心交付，
实际使用 Java、Spring、Redis……
项目包含生产发布、监控告警、故障复盘和量化结果……

早期参与另一个相似岗位相关工作，但不是最近主责方向……
```

目标岗位信号分别放在文本前部、中部或尾部，并加入一个相似岗位作为干扰项。Query 类型轮换为：

- `lexical`：包含目标职位或明显词面信号。
- `semantic_paraphrase`：不直接写目标标题，使用职责和能力描述。
- `hard_negative`：混入相似岗位和技能，测试是否错误召回。

Gold 相关度定义为：

- 精确目标 JD：相关度 3。
- 同类别且标题词组有重合的相似 JD：相关度 1。
- 其余 JD：相关度 0。

### 3.3 局限

- JD 文档是真实公开数据，但 Query 是合成的，不是真实用户简历。
- 弱相关 JD 部分依赖类别和标题词组规则，存在标注偏差。
- 7 个非词法 Query 仍包含精确 Gold 标题。
- 20 个 case 使用职责首句 fallback 弱标注，必须与强标注分开报告。
- 最终配置提升了排序质量，但 `duplicateDocRate@10` 从 0.1025 上升到 0.4875，Top 10 中同一 JD 的多个 Chunk 重复较严重。

## 4. 简历证据召回数据集

### 4.1 规模和内容

- 30 份隐私安全的合成简历。
- 每份简历 4 个 Query，共 120 个。
- 80 个 calibration，40 个 held-out。
- 150 个 Gold spans。
- 覆盖 Java、前端、数据、算法、SRE、安全、产品等岗位族。

每份简历通常包含：

```text
个人摘要
技能
工作经历
核心项目
故障与复盘
教育背景
其他说明/时间线
```

核心项目和故障段落大致如下：

```text
核心项目：
使用 Spring 与 Redis 重构关键链路，峰值吞吐从 900 提升到 2100，
P95 从 480ms 降至 170ms。本人负责瓶颈定位、方案取舍和压测脚本。

故障与复盘：
一次发布后连接池耗尽，请求堆积。37 分钟恢复，
后续增加连接池水位告警、容量演练和发布前检查。
```

### 4.2 Query 和 Gold

每份简历包含四类 Query：

```text
技术预检：Java Spring Redis 项目实践 性能优化 故障排查 量化成果
项目预检：项目 A Spring Redis 架构
故障追问：候选人处理过什么事故？给出定位、止损、恢复和预防动作
时间追问：是否存在超过六个月的空窗，原文有没有解释？
```

Gold 直接标注到当前简历的 Section，例如：

```json
{
  "query": "候选人处理过什么事故？",
  "goldSections": ["project_incident"]
}
```

同一内容还会生成四种排版形态：

- 正常空行段落：8 份。
- 只有换行：8 份。
- 压缩成一整行：7 份。
- 模拟 OCR 噪声：7 份。

### 4.3 局限

简历全部由固定结构和岗位模板生成，答案 Section 比真实简历规整。因此很适合做切分、scope 泄漏和 hard-negative 回归，但 `Recall@5=0.95` 不能直接代表线上真实简历效果。

## 5. 知识库召回数据集

### 5.1 规模和内容

- 19 份 ECS 知识库快照文档。
- 67 个 Query。
- 40 个 calibration，20 个 held-out，7 个 operational。
- 85 个 Gold spans。

文档主要是项目内部的招聘评估知识：

```text
Java 后端工程师评估标准
AI Agent 工程师面试 Rubric
时间线风险判定标准
项目真实性核验清单
评分与推荐一致性规则
英文简历评估补充规范
技术深度信号词典
```

### 5.2 Query 和 Gold

其中 60 个是 Copilot 形态的人工问题：

```text
线程池和 JVM 能力不能只背八股，应该怎么核查？
五千 QPS 的项目数字怎样判断自洽？
两段全职工作重叠三个月属于什么风险？
性能提升 300% 但没有起点数字，能采信吗？
```

每个问题标注正确文档和章节：

```json
{
  "goldDoc": "Java 后端工程师评估标准",
  "goldSection": "二、深度信号"
}
```

另外 7 个是 Workflow 形态的 operational Query：

```text
技术评估 Java Spring Redis 标准
技术评估 RAG LLM Agent 标准
简历评估 评分标准 录用建议 风险判断
```

一个 operational Query 可以对应多份知识文档，因此它们单独报告，不冒充独立 held-out。

### 5.3 局限

- Query 是人工编写或模板生成的，不是真实线上日志。
- held-out 只有 20 条，统计规模较小。
- 文档标题、章节标题和 Query 用词关联较强，当前结果可能偏乐观。
- `Recall@5=1.0` 只能描述冻结测试集，不能解释成对任意真实问题都能 100% 召回。

## 6. 最终联合搜索配置

| 参数 | JD 召回 | 简历证据召回 | 知识库召回 |
|---|---|---|---|
| Chunk | `section_prefix 400/40` | `section_prefix 400/40` | Markdown `section 320/0` |
| Embedding | TE3-768 | TE3-1024 | TE3-768 |
| Tokenizer | CJK bigram | CJK bigram | Domain max-match alias |
| Retrieval | Hybrid | Dense | Hybrid |
| Semantic weight | 0.7 | 1.0 | 0.5 |
| RRF K | 10 | 60（Dense 模式不参与融合） | 10 |
| Threshold | 0.35 | 0.35 | 0.30 |
| Candidate limit | 50 | 5 | 20 |
| Rewrite | Deterministic | None | None |
| Rerank | None | None | None |

Held-out 结果：

| 场景 | Recall@5 baseline → winner | NDCG@10 baseline → winner | MRR baseline → winner | Zero-hit baseline → winner |
|---|---|---|---|---|
| JD | 0.4000 → 0.5750 | 0.3009 → 0.4545 | 0.1915 → 0.2827 | 0.5500 → 0.3000 |
| 简历证据 | 0.5250 → 0.9500 | 0.4960 → 0.8443 | 0.5250 → 0.8208 | 0.4750 → 0.0500 |
| 知识库 | 0.5500 → 1.0000 | 0.4913 → 0.9196 | 0.4396 → 0.8917 | 0.3500 → 0.0000 |

## 7. 可以支持什么结论

这套实验能够支持的结论是：

> 在当前冻结语料和生产形态的合成 Query 上，三类检索任务需要不同配置；经过单因素实验、联合搜索和独立 held-out，最终配置优于当时的生产基线。

它不能支持以下夸大结论：

- 不能证明 `section_prefix` 是所有中文文档的最佳切分方式。
- 不能证明 CJK bigram 普遍优于所有中文分词器。
- 不能把合成简历上的 0.95 Recall 当作真实线上准确率。
- 不能把知识库 20 条 held-out 的 1.0 Recall 外推到任意问题。
- 不能把本地离线检索 P95 当作 Agent Workflow 的端到端延迟。

## 8. 对应项目文件

- 最终实验报告：`reports/rag_three_stage_full_20260805_v2/RAG_THREE_STAGE_EXPERIMENT_REPORT.md`
- 数据门禁：`reports/rag_three_stage/data_gate_gold_v2.json`
- JD 冻结语料：`testdata/rag_three_stage/jd_catalog.json`
- JD Query：`testdata/rag_three_stage/jd_queries.json`
- 简历证据数据：`testdata/rag_three_stage/resume_evidence_cases.json`
- 知识库快照：`testdata/rag_three_stage/knowledge_documents_live.json`
- 知识库 Query：`testdata/rag_three_stage/knowledge_queries.json`
- 实验候选及数据生成：`harness/rag_three_stage_catalog.py`
- 实验执行器：`harness/run_three_stage_rag_experiments.py`
- Gold spans：`testdata/rag_three_stage/rag_gold_spans.json`
