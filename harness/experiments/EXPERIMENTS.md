# 实验注册表（EXP Registry）

原则：**所有无数据支撑的参数不允许直接上线**。每个实验一个可重跑脚本，产物落
`reports/experiments/`；未出结论的参数在代码中标注 `# EXP-N pending` 并使用保守默认值。
实验全部在 ECS 真实环境跑（真实 Milvus / Redis / 百炼 embedding / DeepSeek）。

固定字段：假设 / 变量 / 固定项 / 数据集 / 指标 / 结论 / 上线决策。

| ID | 主题 | 脚本 | 状态 | 结论摘要 |
|----|------|------|------|----------|
| EXP-1 | Embedding 模型选型 | `harness/run_retrieval_benchmark.py --exp embedding` | DONE（2026-07-21） | 同机 A/B：bailian te3-1024 vs local MiniLM-384——KB precision@5 0.6625 vs 0.4625（+20pp）、KB MRR 0.9688 vs 0.8594、JD MRR 1.0 vs 0.8833；**bailian 全面胜出，定为线上默认**。OpenRouter/OpenAI 从本 ECS 网络不可达，无法纳入对照 |
| EXP-2 | 切分策略 × chunk 参数 | `harness/run_retrieval_benchmark.py --exp chunking` | PARTIAL（2026-07-21） | 现行 A-320-60（结构感知 320/60）：KB recall@5=1.0、precision@5=0.66、MRR=0.97；其余网格需逐个重部署分块配置后重跑 |
| EXP-3 | 检索策略与 RRF 权重 | `harness/run_retrieval_benchmark.py --exp strategy` | DONE（2026-07-21） | 全部变体 recall@5=1.0/MRR=1.0（10 例 JD 集未能区分召回）；lexical P95 18ms、hybrid ~24-35ms、vector-only P95 438ms；保持 hybrid-RRF 默认（0.7/0.3），基线已冻结进 CI 门 |
| EXP-4 | Rerank 成本效益 | `harness/run_retrieval_benchmark.py --exp rerank` | DONE（2026-07-21） | rerank 零质量增益（本数据集已满分）且 avg 延迟 24ms→777ms（+753ms）；`rerankerEnabled` 默认 false，仅 agentic 二轮显式开启 |
| EXP-5 | Memory 融合权重 + ablation | `harness/run_memory_ablation.py` | PENDING | 依赖 memory 相关性标注集（尚不存在，需人工标 20+ 条）；融合公式维持 `max(lexical, semantic)`（代码标 EXP-5 pending） |
| EXP-6 | 意图分类置信阈值 | `harness/run_intent_eval.py` | PENDING | 依赖 80 条真实对话意图标注（尚不存在）；`INTENT_CONFIDENCE_FLOOR` 维持 0.7 保守值 |
| EXP-7 | Replan 触发阈值 | e2e benchmark 变体（threshold 扫描） | PENDING | 阈值在 executor 硬编码 0.55（标 `# EXP-7 pending`）；扫描需按阈值重部署 workflow × 3 并全量 e2e，成本高，排期后补 |
| EXP-8 | Function calling vs json_object | `harness/run_json_ab.py` | DONE（2026-07-21） | 各 60 次真实决策调用（emit_decision 生产 schema）：两通道一次通过率均 100%、0 修复 0 失败；FC avg 2645ms vs json_object avg 2030ms（p95 相当）。维持 FC 主通道（provider 端 schema 强制，防御空内容故障模式）+ json_object 兜底，数据表明两者质量无差异 |
| EXP-9 | 进化 reward 权重敏感性 | `harness/run_reward_sensitivity.py` | DONE-INSUFFICIENT_DATA（2026-07-21） | 对真实 policy_reward FEEDBACK 分量做 ±10pp×20 组重加权：现有 2 条反馈行无排名翻转，但样本不足以下稳健结论（报告标 insufficient_data，反馈≥10 条后重跑） |
| EXP-10 | 并行 vs 串行 specialist | `harness/run_parallel_ab.py` | DONE（2026-07-21） | balanced（并行）vs exp10-serial-balanced（仅 parallelSpecialists=false）同 case 真实 e2e 对照；结果见 `reports/experiments/parallel_ab.json` |

## EXP-1 Embedding 模型选型

- 假设：真实大模型 embedding 在中文简历/JD 检索上显著优于本地 MiniLM-384。
- 变量：`local MiniLM-L6-v2 (384)` / `bailian text-embedding-v3 (1024)` /（OpenRouter 系列——网络不可达，无法在本 ECS 测）。
- 固定项：hybrid-RRF 策略、chunk 现状参数、同一 golden set。
- 数据集：`testdata/benchmark/retrieval_cases.json`（10 简历→gold JD + 16 问答→gold 知识块）。
- 指标：recall@5、precision@5、MRR、P95 延迟。
- **2026-07-21 实测（bailian-te3-1024）**：jd_match recall@5=1.0 / MRR=1.0 / P95 22.5ms；knowledge recall@5=1.0 / precision@5=0.6625 / MRR=0.9375 / P95 327ms（首查含远程 embedding；重复查询走 Redis 向量缓存后 P95 33ms，见 EXP-2 行）。
- **2026-07-21 复跑（JD 库 4 篇全量重索引至 `jd_library_bailian_te3_1024` 后）**：jd_match recall@5=1.0 / MRR=1.0 / P95 15.7ms；knowledge recall@5=1.0 / precision@5=0.6625 / MRR=0.9688 / P95 35.9ms（缓存热）。
- **2026-07-21 同机 A/B 对照（线上无流量窗口，切 provider→JD/KB 全量重索引→benchmark→切回）**：

| 指标（同 golden set、hybrid-RRF 0.7/0.3） | local MiniLM-384 | bailian te3-1024 |
|---|---|---|
| jd_match MRR | 0.8833 | **1.0** |
| jd_match P95 | 71.0ms | 49.5ms |
| knowledge precision@5 | 0.4625 | **0.6625**（+20pp） |
| knowledge MRR | 0.8594 | **0.9688** |
| knowledge P95 | 65.2ms | 50.2ms |
| recall@5（两任务） | 1.0 | 1.0 |

  结论：召回率两者打平（数据集内 gold 均能进 Top5），但排序质量差距显著——MiniLM 在中文语义近邻上把 gold 排后（MRR -0.12/-0.11），knowledge precision 低 20pp；bailian 连远程调用延迟都更低（Redis 向量缓存 + 阿里同机房）。**bailian text-embedding-v3 定为唯一线上 provider**（报告：`retrieval_embedding_local-minilm-384.json` / `retrieval_embedding_bailian-te3-1024-rerun.json`）。
- 网络事实：`openrouter.ai` 与 `api.openai.com` 从 ECS（8.138.10.189）TLS 不可达（`[Errno 101] Network is unreachable`）；`dashscope.aliyuncs.com`（~100ms）、`open.bigmodel.cn`（~20ms）、`api.deepseek.com` 可达。
- 上线决策：**bailian text-embedding-v3 / 1024 维**写入 `.env` 与 compose 默认值；KB collection `kb_chunks_bailian_te3_1024` 已全量重索引（15 docs / 121 chunks / 0 failed）。

## EXP-2 切分策略 × 参数

- 假设：结构感知 + 句边界切分优于现状（结构感知 + 字符窗口拦腰切）。
- 变量：策略 A=现状 / B=纯固定窗口 / C=结构感知+句边界 / D=父子 chunk；chunkSize ∈ {256,400,512,768} × overlap ∈ {0,15%,25%}。
- 固定项：EXP-1 胜出 embedding（bailian te3）、hybrid-RRF。
- **2026-07-21 实测（A-320-60 现行配置）**：knowledge recall@5=1.0 / precision@5=0.6625 / MRR=0.9688 / avg 24.8ms。
- 后续：其余组合需按配置逐次重索引后测（每组合一次全量 re-embed），当前召回已满，precision 是后续优化目标。

## EXP-3 检索策略与 RRF 权重

- 假设：hybrid 优于单路；semanticWeight 0.7/0.3 未必最优。
- **2026-07-21 实测（10 JD 例）**：vector-only / lexical-only / hybrid(0.5-0.8) recall@5 全部 1.0、MRR 全部 1.0——本数据集规模不足以区分召回质量；延迟上 lexical avg 11.6ms < hybrid ~20-25ms << vector-only avg 281ms。
- 结论与决策：hybrid-RRF（0.7/0.3）保持默认（单路 lexical 虽快，但在向量可用时 hybrid 提供语义鲁棒性且延迟可接受）；`testdata/benchmark/retrieval_baseline.json` 冻结 recall@5=1.0 进 CI 回归门（跌 >0.02 fail）。扩充标注集（>50 例含难例）后重跑以区分权重。

## EXP-4 Rerank 成本效益

- 假设：LLM listwise rerank 的 ΔMRR 增益值得其延迟/成本。
- **2026-07-21 实测**：hybrid vs hybrid+rerank——recall/MRR 无差（已满分），延迟 avg 23.6ms → 776.7ms（**+753ms**）。
- 决策：`rerankerEnabled` 默认 **false**；仅 agentic 二轮检索（改写后仍低置信）路径显式开启。数据集出现区分度后重估。

## EXP-5 Memory 融合权重 + ablation

- 状态：PENDING——需先建 memory 相关性标注集（人工抽检 20+ 条真实命中）。
- 现行：`MemoryService.search` 融合公式 `max(lexical, semantic)`（代码处标 `EXP-5 pending`）。

## EXP-6 意图分类置信阈值

- 状态：PENDING——需 80 条真实对话消息意图标注（gold）。
- 现行：`INTENT_CONFIDENCE_FLOOR=0.7` 保守默认。

## EXP-7 Replan 触发阈值

- 状态：PENDING——`executor._maybe_replan` 中 `avg_confidence < 0.55` 硬编码并标注 `# EXP-7 pending`。
- 扫描方案：threshold ∈ {0.45,0.55,0.65} 各重部署 workflow 一次 + gold e2e 全量；预算批准后执行。

## EXP-8 Function calling vs json_object

- **2026-07-21 实测**（`harness/run_json_ab.py`，生产 `emit_decision` schema，3 种真实 agent 上下文轮换，deepseek-chat）：

| 通道 | 调用数 | 一次通过 | 需修复 | 失败 | avg 延迟 | P95 |
|------|--------|----------|--------|------|----------|-----|
| function calling | 60 | 100% | 0% | 0% | 2644.5ms | 3383.8ms |
| json_object | 60 | 100% | 0% | 0% | 2030.4ms | 3337.9ms |

- 结论：schema 合规两通道无差异；FC 平均慢 ~614ms。维持 FC 主通道（provider 端强制 schema，历史上防御过 json 模式空内容故障）+ json_object 兜底；若追求延迟可切 json_object 主通道，质量无损。

## EXP-9 进化 reward 权重敏感性

- **2026-07-21 实测**（`harness/run_reward_sensitivity.py`，真实 policy_reward 表分量重加权，10 分量 × ±10pp = 20 组）：
- 现有 FEEDBACK 反馈行仅 2 条（agent_job / deep_analysis），20 组扰动均无 champion 翻转，但样本量不足，报告如实标注 `insufficient_data`。
- 决策：反馈行 ≥10 条后重跑；权重暂维持现值。

## EXP-10 并行 vs 串行 specialist

- **2026-07-21 实测**（`harness/run_parallel_ab.py`）：以 `balanced` 为基线创建 CANDIDATE 策略 `exp10-serial-balanced`（唯一差异 `parallelSpecialists=false`），gold 两例（java-backend-normal / ai-agent-resume）真实 e2e 对照。
- 结果落 `reports/experiments/parallel_ab.json`（avgLatencySeconds / avgReward / token 详见报告）；RESUME_PROJECT_DESCRIPTION 中性能数字引用本产物。
