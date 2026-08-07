---
name: retrieve-public-candidate-evidence
description: 集中定义免密 Exa 和 fetch 对候选人声明 URL 的绑定、超时/限流与 not_checked 契约。仅在简历含显式外链、用户要求公网核验，或项目证据核验需要外部来源时使用。
---

# Retrieve Public Candidate Evidence

把公网证据检索收敛到统一契约，禁止把搜索结果直接写成候选人事实。

## 何时启用

- 简历或用户消息中存在**显式**个人主页 / 博客 / GitHub / Gitee / 作品集 URL。
- `evaluate-candidate-evidence` 产出的项目或技术 claim 需要外部核验。
- 用户明确要求“上网核验 / 打开这个链接”。

无显式 URL 且用户未要求公网核验时：**不要调用**本 Skill 关联工具。

## 工具优先级

1. **stdio fetch**（`fetch.fetch`）：对候选人声明的精确 URL 直接抓取。中国大陆 ECS 对目标可达时这是首选；其描述和参数 schema 必须来自实时 `tools/list`，不使用本地别名。
2. **Exa**（`exa.web_search_exa` / `exa.web_fetch_exa`）：只在用户明确要求发现替代公开来源，或精确 URL 因网络不可达而非 404 失败时兜底。免费 MCP 已限流时立即记 `not_checked`，禁止继续等待或重试。

生产 MCP 清单只允许免 OAuth、免 API Key 的服务；公开 GitHub 页通过 Exa 或白名单 fetch 核验。

### 中国大陆 ECS 路由

- GitHub 连通性按**运行时探测结果**处理，不能因机房地域直接假定可用或不可用。
- GitHub 先按运行时实测走白名单 fetch。明确 404 只说明候选人声明的 URL 当前不可用；网络不可达时才换到 Exa，Exa 限流则记 `not_checked`。
- Gitee / GitCode / CSDN / 掘金 / 知乎 / 博客园等候选人显式声明的国内链接，优先用白名单 fetch 直连；来源 URL 必须原样保留。
- 不得把同名 Gitee 镜像自动当成 GitHub 原仓库；只有简历显式声明或页面提供可验证的 canonical / mirror 关系时才允许绑定。

## URL 绑定与白名单

- 候选事实只允许绑定**候选人声明**的 URL / 标识。
- fetch 白名单域名：`github.com`、`gitee.com`、`juejin.cn`、`zhihu.com`、`csdn.net`、`cnblogs.com`、`medium.com`、`dev.to` 等（以运行时 `FETCH_ALLOWED_HOSTS` 为准）。
- 离白名单域名：本地拒绝，请求不得发出。
- 禁止无目标全网爬取。

## 超时 / 限流 / 失败契约

| 情况 | 输出 |
|------|------|
| 工具成功且带回 source URL | `toolStatus=success`，保留 url/title/publishedAt/provider |
| 超时、熔断、5xx | `toolStatus=failed`，claim 标 `not_checked` |
| 429 / RATE_LIMITED | `toolStatus=unavailable`，claim 标 `not_checked`，可降级到下一工具 |
| 空结果 | `emptyOrFailedResult=unavailable`，禁止合成证据 |

`not_checked` **绝不能**降级为 `unsupported` 或履历造假风险。

## 交付

通过当前 Agent 的统一输出契约提交已核验来源、身份绑定、未检查项和工具健康状态。不要定义、复述或包裹另一套 JSON Schema。

## 边界

- 所有公网结果必须带 `sourceUrl`；无 URL 的片段不得进入候选人证据台账。
- Microsoft Learn 框架文档不是候选人证据，不得经本 Skill 写入。
- ReportAgent 不得直接调用公网 MCP；只消费 Evidence 校准后的 ledger。
