---
name: inspect-github-portfolio
description: 使用免密公网 MCP 对候选人明确声明的 GitHub 公开作品集做来源核验。简历含 GitHub 链接，或用户要求分析仓库质量、贡献和岗位相关性时使用。
allowed-tools: bing_cn.web_search fetch.fetch
---

# Inspect GitHub Portfolio

## 输入

接收用户或简历提供的 profile/repository URL、目标 JD，以及 GitHub 工具返回的结构化结果。

## 流程

1. 验证 URL 和工具状态；未调用或失败时立即返回 `not_checked`。
2. 单独记录 `identityLinkage`：`explicit_resume_link | user_confirmed | unknown`。
3. 引用仓库 URL，检查 README、代码结构、测试、release、issue/PR 和可定位 commit。
4. 将信号映射到具体 JD requirement，不给与岗位无关的活跃度加分。
5. 输出内容证据、身份限制和协作归属限制。

## 输出

```json
{
  "identityLinkage": "explicit_resume_link",
  "repositories": [{"url": "", "signals": [], "requirementIds": [], "sourceRefs": []}],
  "supportedClaims": [],
  "caveats": [],
  "toolHealth": {"bing_cn": "success", "fetch": "not_called"}
}
```

## 证据边界

- 搜索摘要只能辅助定位，候选人事实必须回到带 URL 的真实页面；
  不得直接作为候选人事实，事实必须回到候选人声明的仓库 URL 或可定位源码页面。
- 没有明确关联时不得声称账号属于候选人。
- star、fork、语言占比和提交次数不能单独证明工程质量。
- commit 不能证明全部代码由候选人独立完成，也不能替代工作表现。
- 不生成不存在的仓库、博客或贡献；不读取未获授权的私有内容。
