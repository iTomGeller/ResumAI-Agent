---
name: retrieve-public-candidate-evidence
description: 集中定义免密 Exa、DeepWiki 和 fetch 对候选人声明 URL 的绑定、超时/限流与 not_checked 契约。仅在简历含显式外链、用户要求公网核验，或项目证据核验需要外部来源时使用。
allowed-tools: exa.web_search_exa exa.web_fetch_exa deepwiki.read_wiki_structure deepwiki.read_wiki_contents deepwiki.ask_question fetch.fetch
---

# Retrieve Public Candidate Evidence

把公网证据检索收敛到统一契约，禁止把搜索结果直接写成候选人事实。

## 何时启用

- 简历或用户消息中存在**显式**个人主页 / 博客 / GitHub / Gitee / 作品集 URL。
- `evaluate-candidate-evidence` 产出的项目或技术 claim 需要外部核验。
- 用户明确要求“上网核验 / 打开这个链接”。

无显式 URL 且用户未要求公网核验时：**不要调用**本 Skill 关联工具。

## 工具优先级

1. **Exa**（`exa.web_search_exa` / `exa.web_fetch_exa`）：发现与抓取候选人声明页面。
2. **DeepWiki**（`deepwiki.*`）：仅查询候选人已声明的公开仓库；用仓库结构/内容辅助定位，不把 AI 生成的仓库说明当成独立候选人事实。
3. **stdio fetch**（`fetch.fetch`）：远程 MCP 熔断或限流时的白名单兜底，不承担搜索。其描述和参数 schema 必须来自实时 `tools/list`，不使用本地别名。

生产 MCP 清单只允许免 OAuth、免 API Key 的服务；公开 GitHub 页通过 Exa、DeepWiki 或白名单 fetch 核验。

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

## 输出

```json
{
  "requests": [{"url": "", "provider": "exa", "toolStatus": "success"}],
  "evidence": [{
    "claimId": "",
    "sourceUrl": "",
    "title": "",
    "publishedAt": null,
    "provider": "exa",
    "quote": "",
    "identityLinkage": "explicit_resume_link"
  }],
  "notChecked": [],
  "toolHealth": {"exa": "success", "deepwiki": "not_called", "fetch": "not_called"}
}
```

## 边界

- 所有公网结果必须带 `sourceUrl`；无 URL 的片段不得进入候选人证据台账。
- Context7 / Microsoft Learn 框架文档不是候选人证据，不得经本 Skill 写入。
- ReportAgent 不得直接调用公网 MCP；只消费 Evidence 校准后的 ledger。
