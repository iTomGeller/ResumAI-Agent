---
name: normalize-job-description
description: 将用户粘贴或由真实工具抓取的岗位描述整理为带引用的结构化要求。进行岗位匹配、ATS 检查、岗位比较或面试准备，且存在 JD 文本或岗位页面内容时使用。
---

# Normalize Job Description

## 输入

接收 `jdText`、可选 `sourceUrl`、`retrievedAt`、`locale`。没有正文时不要只根据职位名称补齐要求。

## 流程

1. 提取岗位名、职级、地点、用工类型和职责。
2. 按原文措辞区分 `required`、`preferred`、`responsibility` 和 `unclear`。
3. 将复合要求拆成原子 requirement，并保留原文 quote/span。
4. 标准化技能别名，但同时保留原始写法。
5. 标出模糊范围、冲突条款、缺失职级和可能过期的信息。

## 输出

```json
{
  "role": {"title": "", "level": "unknown", "location": ""},
  "requirements": [{"id": "jd-1", "type": "required", "normalized": "Java", "quote": "熟练掌握 Java", "sourceRef": "jd:l12"}],
  "preferred": [],
  "responsibilities": [],
  "ambiguities": [],
  "source": {"url": null, "retrievedAt": null, "status": "user_provided"}
}
```

## 证据边界

- 只有原文明示的要求才可标为 `required`；不得把常识或相似岗位要求补进当前 JD。
- 抓取失败时标 `not_checked`，不得生成页面内容。
- 不把公司宣传语推断成文化、加班强度或晋升事实。
- 不根据岗位要求推断候选人是否具备能力。
