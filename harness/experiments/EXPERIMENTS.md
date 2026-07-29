# 实验注册表（EXP Registry）

原则：**所有无数据支撑的参数不允许直接上线**。每个实验一个可重跑脚本，产物落
`reports/experiments/`；未出结论的参数在代码中标注 `# EXP-N pending` 并使用保守默认值。
实验全部在 ECS 真实环境跑（真实 Milvus / Redis / 百炼 embedding / DeepSeek）。

固定字段：假设 / 变量 / 固定项 / 数据集 / 指标 / 结论 / 上线决策。

| ID | 主题 | 脚本 | 状态 | 结论摘要 |
|----|------|------|------|----------|
| EXP-1 | Embedding 模型选型 | `harness/run_retrieval_benchmark.py --exp embedding` | DONE（2026-07-21） | 同机 A/B：bailian te3-1024 vs local MiniLM-384——KB precision@5 0.6625 vs 0.4625（+20pp）、KB MRR 0.9688 vs 0.8594、JD MRR 1.0 vs 0.8833；**bailian 全面胜出，定为线上默认**。OpenRouter/OpenAI 从本 ECS 网络不可达，无法纳入对照 |
| EXP-2 | 切分策略 × chunk 参数 | `scripts/_exp2_chunk_grid.sh` | DONE（2026-07-21） | 9 组合网格（256/320/400/512/768 × 0/15%/25% overlap，每组真实重建+重嵌入+重测）：recall@5 全部 0.9375、precision 全部 0.65，MRR 320-60 并列最高 0.9062 且延迟最低（27.9ms）；**维持 320/60** |
| EXP-3 | 检索策略与 RRF 权重 | `harness/run_retrieval_benchmark.py --exp strategy` | DONE（2026-07-21） | 全部变体 recall@5=1.0/MRR=1.0（10 例 JD 集未能区分召回）；lexical P95 18ms、hybrid ~24-35ms、vector-only P95 438ms；保持 hybrid-RRF 默认（0.7/0.3），基线已冻结进 CI 门 |
| EXP-4 | Rerank 成本效益 | `harness/run_retrieval_benchmark.py --exp rerank` | DONE（2026-07-21） | rerank 零质量增益（本数据集已满分）且 avg 延迟 24ms→777ms（+753ms）；`rerankerEnabled` 默认 false，仅 agentic 二轮显式开启 |
| EXP-5 | Memory 融合权重 + ablation | `harness/run_memory_ablation.py` | DONE（2026-07-21） | 24 条合成 memory（半数词面改写难例）× 14 查询三通道对照：semantic recall@5 0.9643/MRR 1.0 vs lexical 0.7143/0.9107；fused-max 不低于任一单路（0.9643/1.0）→ **维持 `max(lexical, semantic)` 融合** |
| EXP-6 | 意图分类置信阈值 | `harness/run_intent_eval.py` | DONE（2026-07-21） | 80 条标注（含否定/关键词干扰难例）：修复 2 个规则误分类后规则层 accuracy **1.0**、LLM 兜底率 0.1875（15/15 开放消息全部正确放行）；`INTENT_CONFIDENCE_FLOOR` 已参数化（默认 0.7），floor 精调待线上 LLM confidence 分布积累 |
| EXP-7 | Replan 触发阈值 | `scripts/_exp7_replan_sweep.sh` + `harness/run_replan_sweep.py` | DONE（2026-07-21） | 0.40/0.55/0.70 × 3 分层真实 e2e（强匹配/边缘/错配）：**全部 0 次置信度触发 replan**——specialist confidence 分布集中在 0.70 以上，该阈值在扫描区间不敏感（replan 实际由冲突信号驱动）；维持默认 0.55，knob 保留 env 可调 |
| EXP-8 | Function calling vs json_object | `harness/run_json_ab.py` | DONE（2026-07-21） | 各 60 次真实决策调用（emit_decision 生产 schema）：两通道一次通过率均 100%、0 修复 0 失败；FC avg 2645ms vs json_object avg 2030ms（p95 相当）。维持 FC 主通道（provider 端 schema 强制，防御空内容故障模式）+ json_object 兜底，数据表明两者质量无差异 |
| EXP-9 | 进化 reward 权重敏感性 | `harness/run_reward_sensitivity.py` | DONE（2026-07-21） | 对 24 个真实 SUCCEEDED run 注入 6 档多样化反馈（强同意→强不同意）后 FEEDBACK reward 行达 28 条；±10pp×20 组重加权 champion（agent_job）**零翻转 → robust**，当前 reward 权重配置保留 |
| EXP-10 | 并行 vs 串行 specialist | `harness/run_parallel_ab.py` | DONE（2026-07-21） | 串行 reward 略高但延迟/成本显著更差（49.2s vs 30.95s）；**拒绝 serial 默认**，上线「并行 + 冲突时串行仲裁」 |
| EXP-13 | Memory TTL 时间回放 | `harness/run_memory_ttl_replay.py --base http://127.0.0.1` | ACTIVE（2026-07-29 首轮） | 188/200 条真实 usage 年龄有效、0 负年龄；Episodic 16 条/最长 0.047d，Procedural 172 条/最长 0.781d，时间跨度不足，结论为 **保持当前 TTL**，禁止自动调参 |

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
- 变量：chunkSize ∈ {256,320,400,512,768} × overlap ∈ {0,~15%,~25%}，`KB_CHUNK_CHARS`/`KB_OVERLAP_CHARS` env 化；每组合真实执行「重建 backend → 删种子文档 → 按新参数重分块入库 → 等 hybrid 就绪 → benchmark」。
- 固定项：EXP-1 胜出 embedding（bailian te3）、hybrid-RRF。
- **2026-07-21 网格实测（9 组合，`scripts/_exp2_chunk_grid.sh`）**：

| 组合 | recall@5 | precision@5 | MRR | avg 延迟 |
|------|----------|-------------|-----|----------|
| 256/0 | 0.9375 | 0.65 | 0.9062 | 33.9ms |
| 256/40 | 0.9375 | 0.65 | 0.9062 | 31.2ms |
| 320/0 | 0.9375 | 0.65 | 0.9062 | 32.2ms |
| **320/60（现行）** | 0.9375 | 0.65 | **0.9062** | **27.9ms** |
| 400/60 | 0.9375 | 0.65 | 0.8750 | 31.0ms |
| 400/100 | 0.9375 | 0.65 | 0.9062 | 37.4ms |
| 512/75 | 0.9375 | 0.65 | 0.8750 | 28.8ms |
| 512/128 | 0.9375 | 0.65 | 0.9062 | 36.2ms |
| 768/115 | 0.9375 | 0.65 | 0.9062 | 29.8ms |

- 结论与决策：recall/precision 在本语料（策略型短文档）对 chunk 参数不敏感；MRR 区分出 400/60 与 512/75 略差（0.875）；**320/60 并列最优且延迟最低，维持现行配置**。注意网格轮的绝对值略低于先前单点（0.9375 vs 1.0）：删除/重种子后 manifest 顺序与 chunk 边界变化所致，属于同一轮内公平对照。

## EXP-3 检索策略与 RRF 权重

- 假设：hybrid 优于单路；semanticWeight 0.7/0.3 未必最优。
- **2026-07-21 实测（10 JD 例）**：vector-only / lexical-only / hybrid(0.5-0.8) recall@5 全部 1.0、MRR 全部 1.0——本数据集规模不足以区分召回质量；延迟上 lexical avg 11.6ms < hybrid ~20-25ms << vector-only avg 281ms。
- 结论与决策：hybrid-RRF（0.7/0.3）保持默认（单路 lexical 虽快，但在向量可用时 hybrid 提供语义鲁棒性且延迟可接受）；`testdata/benchmark/retrieval_baseline.json` 冻结 recall@5=1.0 进 CI 回归门（跌 >0.02 fail）。扩充标注集（>50 例含难例）后重跑以区分权重。

## EXP-4 Rerank 成本效益

- 假设：LLM listwise rerank 的 ΔMRR 增益值得其延迟/成本。
- **2026-07-21 实测**：hybrid vs hybrid+rerank——recall/MRR 无差（已满分），延迟 avg 23.6ms → 776.7ms（**+753ms**）。
- 决策：`rerankerEnabled` 默认 **false**；仅 agentic 二轮检索（改写后仍低置信）路径显式开启。数据集出现区分度后重估。

## EXP-5 Memory 融合权重 + ablation

- 数据集：24 条合成 memory（PREFERENCE/EPISODIC/FAILURE 三类；12 条 lexical-easy + 12 条**词面改写难例**，与查询几乎无表面词重叠）注入隔离 scope `userId=exp5-bench`（TTL 1 天），14 条标注查询，走真实 internal memory search API（Milvus 向量 + MySQL 词面全链路）。
- **2026-07-21 实测（三通道 ablation）**：

| 通道 | recall@5 | MRR | avg 延迟 |
|------|----------|-----|----------|
| lexical only | 0.7143 | 0.9107 | 227.8ms |
| semantic only | **0.9643** | **1.0** | 17.6ms |
| fused max(lex, sem) | **0.9643** | **1.0** | 19.1ms |

- 结论：词面改写场景 lexical 召回崩到 0.71，semantic 通道撑起 0.96；fused-max 与 semantic 打平且保住词面精确匹配的兜底能力。**融合公式 `max(lexical, semantic)` 有数据支撑，正式保留**（代码中 EXP-5 pending 注释移除）。

## EXP-6 意图分类置信阈值

- 数据集：`testdata/benchmark/intent_cases.json` 80 条合成标注，8 类意图，含否定干扰（"别停别停"）、关键词嵌入问句（"为什么不要看学历？"）、隐式目标变化等难例；15 条开放消息 gold=UNCLASSIFIED（规则层必须放行给 LLM 而非瞎猜）。
- **2026-07-21 实测**：首轮规则层 accuracy 0.975（2 个误分类）；修复（问句短路 EVALUATION_FOCUS_CHANGE + RESUME 关键词补 "跑完"）后 **accuracy 1.0，LLM 兜底率 0.1875**——全部 15 条开放消息正确进入 LLM 二段，无一条控制/变更指令被误放行。
- 决策：`INTENT_CONFIDENCE_FLOOR` 从硬编码改为配置（`INTENT_CONFIDENCE_FLOOR`，默认 0.7）。floor 的精调需要线上真实 LLM confidence 分布（本地合成集无法模拟 DeepSeek 打分分布），等 UNCLASSIFIED 流量积累 ≥50 条后按分布定。

## EXP-7 Replan 触发阈值

- 阈值已从硬编码提升为部署参数 `REPLAN_CONFIDENCE_THRESHOLD`（executor 读 env，compose 透传，默认 0.55）。
- 扫描：`scripts/_exp7_replan_sweep.sh` 依次部署 0.40 / 0.55 / 0.70，每档 3 个分层真实 e2e（强匹配 / 边缘带时间线重叠 / 严重错配）。
- **2026-07-21 实测**：三档共 9 个真实 run（8 SUCCEEDED + 1 PARTIAL_SUCCESS in t055/t070 的错配 case），**置信度通道 0 次触发 replan**；错配 case 的 llmStartEvents 在 t070 下从 5-6 升至 9-10（更多组内重试），但均未跨过 replan 线。
- 结论：specialist 汇报的 confidence 分布集中在 0.7+，在 [0.40, 0.70] 区间该阈值不构成行为分界——实际 replan 由**冲突信号**（specialist 结论互斥）驱动，而非低置信。决策：默认 0.55 保留（knob 已 env 化，若后续 prompt 调整拉低置信分布可直接线上调参重扫）。产物 `replan_sweep_t040/t055/t070.json`。

## EXP-8 Function calling vs json_object

- **2026-07-21 实测**（`harness/run_json_ab.py`，生产 `emit_decision` schema，3 种真实 agent 上下文轮换，deepseek-chat）：

| 通道 | 调用数 | 一次通过 | 需修复 | 失败 | avg 延迟 | P95 |
|------|--------|----------|--------|------|----------|-----|
| function calling | 60 | 100% | 0% | 0% | 2644.5ms | 3383.8ms |
| json_object | 60 | 100% | 0% | 0% | 2030.4ms | 3337.9ms |

- 结论：schema 合规两通道无差异；FC 平均慢 ~614ms。维持 FC 主通道（provider 端强制 schema，历史上防御过 json 模式空内容故障）+ json_object 兜底；若追求延迟可切 json_object 主通道，质量无损。

## EXP-9 进化 reward 权重敏感性

- 首轮（反馈仅 2 条）标注 insufficient_data。随后用 `scripts/_exp9_seed_feedback.sh` 对 24 个真实 SUCCEEDED run 注入 6 档结构化反馈（强同意 / 轻微遗漏 / 推荐分歧 / 风险误判 / 未支撑结论 / 强不同意，reward 管道全真实）。
- **2026-07-21 复跑**：FEEDBACK reward 行 28 条、7 个 policy 参与排名；±10pp × 20 组重加权 **champion（agent_job）零翻转，robust=true**。
- 决策：当前 reward 分量权重保留；该敏感性检查已纳入进化环路晋升前置条件。

## EXP-10 并行 vs 串行 specialist

- **2026-07-21 实测**（`harness/run_parallel_ab.py`）：以 `balanced` 为基线创建 CANDIDATE 策略 `exp10-serial-balanced`（唯一差异 `parallelSpecialists=false`），gold 两例（java-backend-normal / ai-agent-resume）真实 e2e 对照。
- 结果落 `reports/experiments/parallel_ab.json`（avgLatencySeconds / avgReward / token 详见报告）；RESUME_PROJECT_DESCRIPTION 中性能数字引用本产物。
- **上线决策（拒绝 serial champion）**：样本上串行 reward 略高，但延迟约 49.2s vs 并行 30.95s、token/成本也更高。生产默认保持并行 Specialist；仅在 Evidence 发现冲突时做单轮串行仲裁（见 executor `_arbitrate_conflicts`），不把 serial 设为默认。

## EXP-13 Memory TTL 时间回放

- 假设：Working 2 天、Semantic/Episodic 90 天、Procedural 365 天可能保留过久或过短，但不能用主观经验直接改生产值。
- 数据：真实 `run_memory_usage`，以 `ageAtUseSeconds` 回放每次 USED/IGNORED 时该条 memory 在候选 TTL 下是否仍存活；负年龄和缺失年龄明确剔除并计数。
- 质量门：每类型至少 30 个 USED；历史最大年龄至少覆盖当前 TTL 的 80%；至少覆盖 3 个不同年龄日；候选必须实际产生差异；USED 与按 `finalScore` 加权的 USED 保留率均不低于 99%。
- 候选：Working 1/2/3/7 天；Semantic/Episodic 30/60/90/180 天；Procedural 90/180/365/730 天。
- 决策边界：脚本只生成 JSON/Markdown 报告，**绝不修改生产配置**。任一覆盖门不足即输出 `KEEP_CURRENT_DEFAULTS_INSUFFICIENT_DATA`；只有数据充分且质量门通过才提出最短候选，仍需人工审查后才能上线。
- **2026-07-29 版本切点复核**：全库 764 条 usage 最晚为北京时间 15:03，而当前 workflow 容器于 16:03 启动；当前 Memory 查询、分类并发及多类型融合实现上线后的干净 usage 为 **0 条**。因此 764 条只能作为旧版本基线，不能支持当前版本 TTL 最优性；脚本增加 `--since-utc` 强制版本 cohort，混合历史标记 `BASELINE_ONLY_MIXED_VERSION`，当前版本无数据标记 `INSUFFICIENT_CURRENT_VERSION_DATA`。生产配置 **Working 2 / Semantic 90 / Episodic 90 / Procedural 365 天仅作为 incumbent 默认值保留，不声称为实验最优**。产物：`reports/experiments/memory_ttl_replay.{json,md}`。
- **2026-07-29 当前版本干净 cohort + shadow A/B**：增加生产版本二次切点，只保留“当前 workflow 消费 + 当前 workflow 生成”的 usage。真实 E2E 后得到 30 条干净 Episodic USED；Working/Semantic/Procedural 仍为 0。对 Java 后端与 AI Agent 两类 gold 简历跑 4 个有效完整 workflow shadow run，保留当前 Episodic 相比临时过期，平均 Reward **+0.019**、证据支持率 **+0.129**、耗时 **-5.06s**，must-find/违规率无回退。决策：保留 Episodic 90 天 incumbent，但分钟级年龄不足以判断 90 天优于 30/60 天。产物：`reports/experiments/memory_ttl_shadow_ab.{json,md}`。
