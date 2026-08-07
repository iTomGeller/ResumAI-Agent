# 三阶段 RAG 专项实验协议

本协议以当前代码调用点和 ECS 语料形态为准。三个阶段分别实验，不共享一个“全局最佳”配置。

## 1. 真实调用链与参数适用性

| 阶段 | 真实入口 | 查询 | 语料/作用域 | 当前检索 | 当前切分 | 当前 rewrite | 当前 rerank |
|---|---|---|---|---|---|---|---|
| JD 岗位召回 | `Coordinator -> jd_match_search -> /api/internal/tools/jd-search` | 完整 `resumeText`；向量截前 2000 字，BM25 截前 3000 字 | MySQL `jd_library` + Milvus JD collection | 文档级标准 BM25 + 按 JD 去重后的 dense + weighted RRF | 索引时固定 `recursive(400,80)`；JD 是 textarea 普通文本，代码仅前置“岗位/类别” | 无 | 线上关闭；代码可选 DeepSeek listwise |
| 当前简历证据 | `Tech/Project/Report -> resume_semantic_search -> /api/internal/tools/resume-search` | workflow 模板 query 或 Copilot 原问题 | 仅本次请求的 `resumeText`；禁止跨候选人 | section 规则 + BM25-like phrase contains + RRF | 空行分块；只有一个块时退化按行；返回片段截 600 字 | 只有 followup/quick_answer 请求 rewrite，但实现原样返回 | controller 固定 overlap-density + length 规则 |
| 评估知识库 | `Tech/Report -> knowledge_search -> /api/rag/knowledge-base/search` | workflow 模板 query 或 Copilot 原问题 | ECS 当前 19 文档/147 chunks | weighted contains（代码名 BM25-like，但非标准 BM25）+ Milvus + RRF k=60 | Markdown 标题和每个编号条目先作为边界；超长段再按 320/60 字符窗 | followup/quick_answer 标记 rewrite，但实现原样返回 | workflow 传 `rerank=true`，实际为 `feature_rerank_v1` |

结论：embedding 模型/维度对当前简历证据阶段的线上现状是 `N/A`。实验中可增加“仅当前候选人作用域的 dense”架构候选，但不得描述成当前实现，也不得使用全局简历 Milvus。

## 2. 冻结语料、划分与原文证据

- JD：120 条 Apache-2.0 真实中文 JD，短/中/长各 40 条，覆盖 8 个岗位类别；全部来自普通 textarea，无 Markdown。120 个 production-shaped query 按 80 calibration / 40 held-out 划分。
- 当前简历证据：30 份隐私安全的合成简历、120 个 query，覆盖空行段落、逐行文本、压缩单行和 OCR 噪声 4 种 PDF 提取形态。按简历分组为 80 calibration / 40 held-out，同一份简历绝不跨 split。
- 知识库：ECS 导出的 19 份真实 live 文档。60 个 Copilot 问题分为 40 calibration / 20 held-out；7 个 workflow 模板查询单列为 operational，不伪装成独立 held-out。
- 正确答案不是 docId 或 section 名称提示，而是冻结在原文字符区间上的 gold spans：JD 279 个、简历 150 个、KB 85 个。数据门禁校验正文哈希、span 文本哈希、字符边界、来源、许可证和 split 隔离。
- 20 条 JD 没有可由词典稳定定位的技术词，使用 `deterministic_duty_lead_fallback_v1` 职责首句弱标注；必须单列 cohort 和置信区间，不能与强标注混在一个结论中。

## 3. 切分实验

JD：whole 仅作负对照；固定字符窗、当前 recursive(400,80)、中文标点边界、普通中文标签识别、标签+title 前缀，以及相邻句 embedding topic-break 的 semantic / section-semantic 方案。

当前简历：whole 仅作负对照；当前空行/行逻辑、固定字符窗、中文标点边界 256/320/500、普通简历标签、标签+title 前缀、semantic 和标签约束 semantic。所有 dense 候选只在单候选人作用域内。

知识库：whole 仅作负对照；精确复刻当前 numbered-boundary 256/320/400/512、中文标点边界、Markdown heading、heading+title 前缀、heading 约束 semantic。章节之间不做 overlap；只允许过长章节内部 overlap。

Semantic 切分参数包括 target/max/min 字符数、相邻句距离断点 percentile 和句子 overlap。切分用的模型/维度也进入联合 tuple，因此会与检索 embedding 发生真实交互，不能作为脱离模型的固定预处理结论。

每个候选记录：chunks/doc、长度 p50/p95/p99、短块率、跨章节率、overlap 重复率、title 前缀率、估算向量字节数和索引耗时。

## 4. 中文 sparse 实验

JD 使用标准 BM25，比较：当前英文词+中文 bigram、单字+bigram、jieba、领域最长词+别名归一化。

当前简历先复刻当前“整段 phrase contains”作为 baseline，再比较 true BM25 的 bigram、jieba、领域词典。

知识库先复刻当前 title/section/content/tags contains 加权作为 baseline，并明确它不是 BM25；再比较 true BM25 的 bigram、jieba、领域词典。

别名只做确定性归一化，例如 `K8s/Kubernetes`、`大模型/LLM`、`智能体/Agent`、`检索增强生成/RAG`，不生成候选人不存在的事实。

## 5. Dense、融合、rewrite、rerank

- Embedding：`text-embedding-v3` 256/512/768/1024；`text-embedding-v4` 512/1024。模型不可用时记录 API 错误并排除，禁止静默替换。
- Fusion：lexical、dense、hybrid；semantic weight 0.3/0.5/0.7；RRF k 10/30/60/100。
- Rewrite：none、确定性结构化提取/别名归一化、DeepSeek JSON rewrite。当前 passthrough 归入 none，不冒充 query rewrite。
- Rerank：JD 为 none/DeepSeek listwise/Qwen3；当前简历为 none/当前 overlap-density/Qwen3/DeepSeek；知识库为 none/当前 feature rerank/Qwen3/DeepSeek。

## 6. 实验设计、指标与选择门槛

单变量实验只在 calibration 上解释每个因素的作用和缩小搜索空间，不直接宣布最终 winner。最终配置由完整参数 tuple 联合搜索决定，tuple 同时包含切分方式和参数、semantic 切分模型、检索 embedding 模型/维度、中文 sparse 分词、dense/sparse/fusion、阈值、候选数、RRF、rewrite 和 rerank。

联合搜索使用固定 seed 的随机候选与 successive halving：先在 calibration 子集粗筛，再让 finalists 跑完整 calibration。Pareto frontier 同时考虑检索质量、生成模型调用数、索引体积和跨语义段混合率。只有选定 tuple 和当前生产 baseline 可以查看 held-out；知识库 workflow query 只报 operational 结果。

质量：Recall@1/3/5/10、Precision@1/3/5/10、MRR、MAP、nDCG@5/10、zero-hit、hard-negative FP@5、正负 score margin。证据指标按 gold-span union coverage 计算；同一 span 被多个重叠 chunk 命中只计一次 gain，避免 nDCG 大于 1。

专项：JD 按 plain format、信号 early/middle/late 分层；简历 evidence recall 和 candidate-scope leakage；知识库 document recall 与 evidence chunk recall，并按 workflow/Copilot query 分层。

性能/成本：rewrite、sparse、dense、fusion、rerank、total 的 p50/p95/p99；模型调用数、cache hit、输入字符/token；索引时间、向量字节；ECS Docker CPU/RSS/网络/重启/OOM 每 2 秒采样。

选择规则：质量效用领先；差值不超过 0.005 时优先更小索引和更低复杂度。rewrite/rerank 相对 none 的效用提升不足 0.01 时不上线。held-out 上用 2,000 次 paired bootstrap 报总体和 cohort 置信区间；JD 按长度、信号位置、标注强弱，简历按 layout/query source，KB 区分文档命中和证据定位。禁止只报平均数。

## 7. 结果审计与复现

- 每个配置保留逐 query 的 fusion、dense、sparse Top-K chunk，审计 evidence coverage/purity、正文范围、重复、跨候选人泄漏和跨段混合。
- 审计门槛为 `evidenceCoverage >= 0.50` 且 `evidencePurity >= 0.35`；自动分数不能替代对 finalist 失败样本和关键命中 chunk 的人工复核。
- ECS 默认执行 48 个联合 tuple、10 个 finalist，并记录 Linux/Docker CPU、RSS、网络、重启/OOM、各阶段延迟、API 调用次数和缓存命中。
- 外部 embedding/rewrite/rerank 不可用时显式记为 `unavailable`，禁止静默替换模型。Windows 只做数据门禁和无 Key smoke，正式结论必须来自 ECS。
