---
name: handle-knowledge-no-evidence
description: 规定知识库检索全部 fallback 耗尽时只能回答“无知识库证据”或请求补充，不得用模型常识冒充命中。Copilot 追问、ReportAgent RAG、knowledge_search 空结果时使用。
allowed-tools: knowledge_search
---

# Handle Knowledge No Evidence

当知识库分层 fallback 耗尽仍无可用 chunk 时，强制走“无证据”回答路径。

## 触发条件

- `knowledge_search` / 混合检索返回 `chunks=[]` 且 `noEvidence=true`。
- `fallbackStage=exhausted` 或等价耗尽信号。
- topScore / 命中数不足且 rewrite + 放宽阈值 + title/tag fallback 均已尝试。

## 必须做的事

1. 明确告知：**本次知识库未命中可用评估标准/文档**。
2. 可以基于**已有简历原文、JD 文本、用户补充事实**继续回答，并标注来源是简历/JD/用户而非知识库。
3. 需要标准却缺失时，**请求用户补充**文档或澄清问题，而不是猜测。
4. 在报告/回答中保留 `noEvidence` / `fallbackStage` / `reason`，供 Trace 审计。

## 禁止做的事

- 用模型参数知识、训练语料或“行业常识”**冒充**知识库命中。
- 伪造 `chunkId`、`docId`、`citation`、`score`。
- 把低分噪声 chunk 硬塞进上下文后假装“命中了标准”。
- 在无证据时输出“根据知识库规定……”类措辞。

## 推荐话术模板

```text
知识库未检索到与该问题相关的评估标准或文档（fallback 已耗尽）。
我可以基于当前简历/JD 中的可定位事实回答如下；若需要对照内部标准，请补充相关文档或更具体的问题。
```

## 输出

```json
{
  "noEvidence": true,
  "fallbackStage": "exhausted",
  "reason": "lexical+vector+rewrite+metadata fallback empty",
  "answerMode": "resume_jd_only_or_ask_user",
  "citations": [],
  "userAsk": "可选：请补充相关评估标准文档"
}
```

## 与其他 Skill 的关系

- `calibrate-evidence-confidence`：KB 空结果记为 `toolStatus=unavailable` / claim `not_checked`，不降为 `unsupported`。
- `explain-evaluation-decision`：解释评分时不得引用不存在的知识库条目。
- `audit-job-relevant-evaluation`：无 KB 标准时只能用 JD/简历证据，避免偏见补全。
