# Memory TTL 实验与 100 份简历压测监测核对

## 1. 先说结论

当前业务 Memory 只保留两层：

| Memory 类型 | 作用 | 默认 TTL |
|---|---|---:|
| `RECENT_CASE` | 同岗位近期的脱敏评估案例，每次最多召回 2 条 | 30 天 |
| `JOB_PROFILE` | 同岗位、同 JD fingerprint 的累积画像，每次最多召回 1 条 | 180 天 |

`30/180 天` 不是拍脑袋值，而是从预先声明的 TTL 候选网格中，选出第一个通过业务覆盖门槛的最短 TTL。

但必须说清楚：这是一次**受控时间回放实验**，验证的是“设定 TTL 后，同岗位 Memory 还有多大概率可用”，不是“Memory 必然能让最终报告提分”。

## 2. 为什么不直接用线上压测时间

当时线上的 Memory 基本都由压测在很短时间内生成。如果直接用这些时间戳比较 `7/14/30/90/180` 天，所有记忆都还没过期，不同 TTL 的结果几乎一样，实验无法做出选择。

因此实验没有修改线上 `memory_entry`，而是使用仓库已有的 100 份压测简历，只模拟它们在 365 天内的到达时间。

## 3. 实验输入如何构造

### 3.1 数据集

- 来源：`testdata/stress_resumes/manifest.json`
- 简历：100 份
- 岗位 cohort：15 类
- 时间范围：模拟 365 天
- 随机种子：40 组，对结果取平均
- 线上数据变更：无

岗位 cohort 由用例 ID 去掉末尾序号得到。例如：

```text
senior_backend_001
senior_backend_002
        ↓
senior_backend
```

它不依赖候选人姓名、手机号或用户会话，因为当前两层 Memory 本来就是按岗位隔离、脱敏复用的。

### 3.2 案例相似度

实验从 manifest 中取出三类特征：

```text
expectedSkills
简历长度桶：short < 1200，medium < 2200，否则 long
是否包含 GitHub 公开证据
```

两份同岗位案例使用 Jaccard 计算特征相似度：

```text
similarity = |交集| / |并集|
```

注意：这不是线上 Embedding 或 Milvus 的语义分数。它只是为 TTL 回放提供一个固定、可重现的案例相似性代理指标。

### 3.3 模拟三种岗位流量

每个岗位独立生成 Poisson 到达流，使用指数分布产生两次候选人评估的时间间隔：

| 流量档位 | 每岗位平均到达频率 | 含义 |
|---|---:|---|
| sparse | 0.25 份/周 | 约四周一份 |
| normal | 1 份/周 | 每周一份 |
| busy | 5 份/周 | 每个工作日一份 |

## 4. `RECENT_CASE=30 天` 怎么选出来

### 4.1 候选网格

```text
7, 14, 30, 45, 60, 90 天
```

每次查询最多选择同岗位中相似度最高的 2 条近期案例。

### 4.2 事先声明的选择门槛

一个 TTL 必须同时满足：

```text
normal 流量 Top-2 覆盖率 >= 0.90
busy   流量 Top-2 覆盖率 >= 0.95
normal 流量平均案例相似度 >= 0.90
```

如果多个 TTL 通过，选最短的一个，减少陈旧案例暴露和存储。

### 4.3 决策边界

| TTL | normal Top-2 覆盖 | busy Top-2 覆盖 | normal 平均相似度 | 是否通过 |
|---:|---:|---:|---:|---|
| 14 天 | 0.5795 | 0.9956 | 0.8750 | 否 |
| 30 天 | 0.9112 | 0.9961 | 0.9182 | 是，首个通过 |
| 45 天 | 0.9679 | 0.9961 | 0.9499 | 是，但不是最短 |

因此选择 `RECENT_CASE=30 天`。

sparse 流量在 30 天下 Top-2 覆盖只有 `0.2640`。这不是被隐藏的异常：如果一个岗位约四周才有一份简历，就不能期望在 30 天内始终凑齐两条历史案例。当前决策接受这个边界，没有为了稀疏岗位把所有案例保留 90 天。

## 5. `JOB_PROFILE=180 天` 怎么选出来

### 5.1 候选网格

```text
30, 60, 90, 180, 365 天
```

一个岗位至少积累 3 个案例后才认为岗位画像已建立。建立之后，实验检查下一次同岗位评估时该画像是否仍在 TTL 内。

### 5.2 选择门槛

```text
min(sparse 可用率, normal 可用率, busy 可用率) >= 0.99
```

同样选择首个通过的最短 TTL。

### 5.3 决策边界

| TTL | sparse | normal | busy | 最低可用率 | 是否通过 |
|---:|---:|---:|---:|---:|---|
| 90 天 | 0.9610 | 1.0000 | 1.0000 | 0.9610 | 否 |
| 180 天 | 0.9989 | 1.0000 | 1.0000 | 0.9989 | 是，首个通过 |
| 365 天 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 是，但不是最短 |

因此选择 `JOB_PROFILE=180 天`。

## 6. 实验结论如何落到代码

后端实际默认值：

```java
private static final Map<String, Duration> TTL_BY_TYPE = Map.of(
    "RECENT_CASE", Duration.ofDays(30),
    "JOB_PROFILE", Duration.ofDays(180));
```

写入时如果没有单条 `ttlDays` 覆盖，就使用上述默认值计算 `expiresAt`。当同内容去重命中时，会从当次更新时间重新计算过期时间。

`JOB_PROFILE` 还受 JD fingerprint 约束。JD 内容变更后，旧画像不能仅因为 TTL 没到就被注入新任务。

## 7. 之前的 100 份简历压测有没有 Memory 指标

有，而且当时采集得比较细。但这些报告都产生于旧四类 Memory 时期：

```text
WORKING / SEMANTIC / EPISODIC / PROCEDURAL
```

它们不能直接当作当前 `RECENT_CASE/JOB_PROFILE` 两层架构的 100 份压测证据。

### 7.1 当时已采集的指标

| 类别 | 具体指标 |
|---|---|
| 产出 | Memory 总数，按 type/scope/source/status 分组 |
| 消费 | `USED/IGNORED`，按 Memory 类型、Agent、来源分组 |
| 召回 | `memory.read/selected/missed/used/written` 事件数、有命中的 read 比例、返回片段数 |
| 排序 | vector/lexical/recency/final score，以及 final score 分位数 |
| 时间 | Memory 被使用时的年龄，各类型实际 TTL 与剩余 TTL |
| 延迟 | Java Memory search 边界的 min/avg/P50/P95/P99/max |
| 版本 | producer/consumer workflow 版本不一致数量 |
| 策略 | 使用默认 TTL 还是写入时 override |

### 7.2 三次代表性 100 份报告

| 报告 | Memory reads | read hit rate | 返回命中 | USED | 检索 P50 / P95 | 版本不一致 |
|---|---:|---:|---:|---:|---:|---:|
| `final_100_f89ab83_20260731` | 400 | 42.00% | 236 | 1129 | 37 / 56 ms | 0 |
| `final_100_eager_report_20260801` | 388 | 39.69% | 227 | 1117 | 22 / 32 ms | 0 |
| `project_cache100_20260803` | 400 | 39.50% | 258 | 1240 | 28 / 43.05 ms | 0 |

更早的 `load_100_ingress1qps_278dad8_20260730` 还记录到 `962` 条 producer/consumer 版本不一致；后续上表三次都已经是 `0`。这说明版本监测确实暴露过问题，不是空字段。

## 8. 监测的真实缺口

现有历史指标能回答“有没有查、查到几条、给了哪个 Agent、多慢、TTL 是多少”，但不能回答以下问题：

1. 注入 Memory 后，最终报告质量相比禁用 Memory 提升了多少。
2. 命中的 Memory 是否真正被 LLM 用于某个具体结论，而不只是注入了 Prompt。
3. 是否存在“相似但不适用”的负迁移，或旧风险被错用到当前候选人。
4. 当前两层 Memory 在 100 份完整 Workflow 下的独立命中率、分 Agent 消费率与下游质量收益。
5. 每条 Memory 实际增加了多少 Prompt token。

还有两个明确的版本债：

- `harness/analyze_load_test.py` 生成 Markdown 类型表时仍硬编码旧四类 Memory；JSON 的 `byType` 可以动态收集新类型，但报告表格会漏掉 `RECENT_CASE/JOB_PROFILE`。
- Ops 页面 `MemoryInspector.vue` 仍展示 Semantic/Episodic/Procedural 的旧文案，与当前两层架构不一致。

因此当前最准确的说法是：

> 两层 Memory 的 TTL 已经过受控覆盖实验并落到代码；历史 100 份压测有较完整的 Memory 运行监测，但属于旧四类架构。当前还缺一次针对 `RECENT_CASE/JOB_PROFILE` 的 100 份回归和 Memory on/off 质量对照。

## 9. 可追溯资料

- 两层 TTL 实验脚本：`harness/run_business_memory_ttl_experiment.py`
- 实验结果：`reports/experiments/business_memory_ttl_controlled.json`
- 实验简报：`reports/experiments/business_memory_ttl_controlled.md`
- 线上 TTL 实现：`backend/src/main/java/com/resumai/agent/service/MemoryService.java`
- 两层类型迁移：`backend/src/main/resources/db/migrations/V22__business_memory_layers.sql`
- 压测 Memory 指标收集：`harness/analyze_load_test.py`
- 历史指标样本：`reports/final_100_f89ab83_20260731/memory_metrics.json`

## 10. 历史四类 TTL 实验的位置

仓库中还有 `reports/experiments/memory_ttl_controlled.md`，其结论是：

```text
WORKING=1d
SEMANTIC=90d
EPISODIC=90d
PROCEDURAL=365d
```

这是旧架构的 EXP-14，不是当前两层 Memory 的 TTL 来源。当前面试或文档应以 `RECENT_CASE=30d / JOB_PROFILE=180d` 为准，不应将两套结论混用。
