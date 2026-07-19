# 测试报告（ECS 真实执行，2026-07-19 收敛式重构）

执行环境：阿里云 ECS 4C16G（Ubuntu, Docker 29.1.3），项目目录 `/opt/resumai-src`，
Compose project `resumai`，模型 deepseek-chat。

## 架构验收（旧 Runtime 删除）

- 带内部 Token 请求旧接口：`POST /workflow/runs`=404、`POST /execute`=404、
  `GET /workflow/runs/{id}`=404、`POST /workflow/runs/{id}/control`=404。
- 全仓库 grep：`/workflow/runs`、`app.graph`、`default_run_registry`、
  `run_workflow` 零残留（CI 含防回流门禁）。
- 旧 checkpoint PostgreSQL 容器已停用；卷 `resumai-workflow-postgres-data`
  保留在磁盘未删除。V7 迁移已应用（pause/resume 快照 + resume_task 桥接）。
- Sandbox Worker 镜像按 Git SHA 固定（部署时写入 `SANDBOX_WORKER_TAG`），
  运行中 Manager 环境变量已验证非 latest。

## 单元 / 集成测试

- Java（ECS, JDK21）：8 类 23 用例全部通过。
- Python：56 用例通过（含并行分组、pause/resume 快照往返且已完成 Agent 不
  重跑、toolCallId 配对压缩、PARTIAL_SUCCESS 语义、契约基准无 champion）。
- 契约门禁 `run_agent_harness.py`：18 项全 PASS（构建期强制执行）。

## 功能验收（真实 LLM + Sandbox）

- 完整评估：SUCCEEDED，7 个 Agent 全执行（含 ReportAgent），LLM 8 次、
  工具 4 次、token 12190+7980、时延 46.2s、degraded 空。
- 每 Agent 真实耗时（ms）：Parser 5240 / JD 2174 / Tech 11844 ∥ Project 11811 ∥
  Risk 11783（并行组）/ Evidence 8756 / Report 15958。
- PAUSE：RUNNING → PAUSING → PAUSED，快照落库（executedAgents=[ResumeParserAgent]）；
  RESUME 后完成，快照内已完成 Agent 精确执行 1 次（不重跑）。
- INTERRUPT：运行中打断 → CANCELLED，取消传播到 Python task。
- resume_task 桥接：上传任务经 /agent/runs 执行，agent_run.source_task_trace_id
  关联，任务状态/摘要回写（35 秒完成；一次验收轮询窗口过短导致的误报已复核）。

## 性能优化前后（同一完整评估用例，真实数据）

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| LLM 调用 | 18 次（预算耗尽降级） | 8 次 |
| 总时延 | 139.7s | 46.2s |
| 结果状态 | PARTIAL_SUCCESS（budget:maxLlmCalls） | SUCCEEDED |
| ReportAgent | 未能执行 | 正常收尾 |

手段：Specialist 并行、简单请求纯规则路由、终端 Agent 接受原始 markdown、
失败的终端 Agent 不再重排、预置工具产物复用（parsedResume/jdCoverage）。

## 真实 Agent E2E Quality Benchmark（e2e-20260719-170639-b0e9a87d）

3 个 Gold 用例 × 3 策略 × 3 次重复 = 27 次真实 /agent/runs，全部 SUCCEEDED；
指标全部来自 runtime metrics（真实 LLM 次数、真实 token、按官方单价计的成本）。

| Policy | Reward | Success | LLM | Tokens(P/C) | Cost(CNY) | Avg | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| balanced | 0.3725 | 100% | 6.44 | 10734/6809 | 0.0759 | 37.6s | 49.3s |
| strict_evidence | 0.3529 | 100% | 6.56 | 10559/5971 | 0.0689 | 35.4s | 50.5s |
| **low_cost（Champion）** | **0.4629** | 100% | 3.67 | 4431/2911 | 0.0322 | 22.4s | 33.7s |

Champion 已写回 `policy_bundle.is_champion`。评估标签只进评估器；固定策略
以 FORCED 模式记录（27 条 FORCED selection，与 EXPLOIT/EXPLORE 分离）。

Contract Benchmark（19 用例 × 7 策略）独立输出于 `reports/benchmark/contract/`，
仅验证工具契约/公式/安全规则/故障注入，不产生质量结论。

## 可靠性与持久化

- 重启（backend+workflow+sandbox-manager）：conversation 39→39、agent_run
  44→44、resume_task 205→205；重启后无 RUNNING/STARTING 残留。
- Memory 生命周期：CONVERSATION 33、EPISODIC 44、WORKING(RUN) 20、
  FAILURE(GLOBAL) 6、HR_FEEDBACK 2 —— 每类均有真实写入且 scope 隔离。
- 原有 named volumes 全程复用；部署前 mysqldump 备份；行数前后校验内置。

## 本轮发现并修复

1. `MutableTask.revisionNo` 原始类型判空编译错误（桥接代码）。
2. ReportAgent 长报告溢出 JSON 包装反复失败烧尽预算：改为接受原始 markdown
   （报告即交付物），且失败的终端 Agent 不再重排。
