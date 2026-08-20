# Agent JSON 输出保障与失败修复机制

> 对应当前线上 `RunExecutor + ResilientLlmClient` 实现。本文只描述项目真实代码，不把 Prompt 约束包装成“模型一定不会出错”。

## 1. 一句话结论

系统不能保证 LLM 每次第一次就生成合法 JSON；系统真正保证的是：

> **未经语法解析、结构校验和报告语义校验的输出，不会被当作有效 AgentOutput 或最终报告写入 SharedState。**

模型输出错误时，Runtime 会携带精确错误进入一个禁用 Tool 的 JSON-only 修复轮；预算耗尽仍不合法时，明确失败或走受限降级，不会用字符串替换、补括号等方式伪造一份“看似合法”的结果。

## 2. 正常输出链路

```text
Prompt 中声明输出契约
    ↓
Provider 原生 Function Calling + JSON Schema
    ↓
解析原始 function arguments
    ↓
Pydantic AgentDecision 校验
    ↓
ReportAgent 额外业务语义校验
    ↓
生成 canonical AgentOutput / finalReport
    ↓
最后再由确定性代码渲染 Markdown
```

当前 Specialist 使用 `emit_decision`。ReportAgent 使用更严格的报告 schema，但线上函数名目前也叫 `emit_decision`；逻辑上它是报告终态提交函数，并没有另一个真实的 `emit_report` Tool。

## 3. 第一层：尽量让模型第一次就输出正确

### 3.1 Prompt 只保留人能理解的契约

Specialist Prompt 要求：

```json
{
  "thought": "简要计划",
  "output": {
    "summary": "一句话结论",
    "claims": [],
    "evidence": []
  },
  "done": true
}
```

真正的字段类型、必填项、枚举和长度限制以 Function Schema 为准，避免 Prompt 和代码维护两套互相矛盾的 JSON 定义。

### 3.2 用原生 Function Schema 约束输出

Runtime 向模型暴露 `emit_decision`：

- `done` 必须是 boolean；
- `output` 必须是 object；
- `claims`、`evidence` 必须是 array；
- Report 的 recommendation 只能是四个枚举值；
- Report 要求 4 个维度、2～4 个优势、1～4 个风险、4～6 个面试问题；
- 分数限制在 0～100；
- 证据来源限制为 `RESUME/JD/KNOWLEDGE/EXTERNAL`；
- 字符串、数组和 evidenceRefs 都有长度或数量上限。

Tool 观察阶段允许 `tool_choice=auto`。当 Action Round 用完或到最后一轮时，Runtime 强制：

```json
{
  "type": "function",
  "function": {"name": "emit_decision"}
}
```

这能显著降低自由文本、Markdown 包裹和漏字段概率，但不能认为 Provider 绝对不会返回残缺的 arguments；20 份实测仍观察到 5 个修复轮。

## 4. 第二层：严格解析，不偷偷“修 JSON”

Provider 返回的 `raw_arguments` 会原样保留用于审计。Runtime 使用 `json.loads()` 解析，并把错误保存为 `arguments_error`。

例如模型返回：

```text
{"done":true,"output":{"summary":"符合岗位" "claims":[]}}
```

中间少了逗号，Runtime 会得到类似：

```text
Expecting ',' delimiter: line 1 column ...
```

Runtime 不会自动插入逗号。原因是字符串级“修复”可能改变字段边界、证据内容或数值含义，无法证明修复后的内容仍是模型原意。

兼容路径只做两种不改变语义的处理：

1. 从 Markdown code fence 或前后少量说明中提取第一个完整 JSON object；
2. 如果某些兼容 Provider 把 `output` 二次编码为完整 JSON 字符串，再按同一解析器解码一次。

找不到闭合对象、存在非法语法或 `output` 字符串内部不是完整 JSON 时，仍判失败。

## 5. 第三层：Pydantic 结构校验

合法 JSON 不代表符合 Agent 协议。例如：

```json
{
  "done": "yes",
  "output": []
}
```

它能被 JSON parser 解析，但不符合 `AgentDecision`：

- `done` 应为 boolean；
- `output` 应为 object 或 null；
- `toolCalls.arguments` 必须是 object；
- 下游收到的必须是经过 `model_validate()` 的普通 dict。

Pydantic 返回的具体 validation error 会原样截取并交给下一次修复调用，而不是只告诉模型“格式错了”。

## 6. 第四层：ReportAgent 业务语义校验

ReportAgent 即便通过通用 `AgentDecision`，还要通过更严格的报告检查：

- 必须有 `output.report`；
- 必须包含 recommendation、dimensions、strengths、risks、interviewProbes、dataQuality；
- dimensions、strengths、risks、interviewProbes 必须是数组；
- recommendation、status、severity 等必须属于约定枚举；
- 分数必须在 0～100；
- `UNASSESSED` 的 score 必须为 null；
- 无效证据引用、控制面错误混入候选人风险等内容会被过滤；
- dimensions 全部无效或报告未通过运行时语义校验时，不能结束 ReportAgent。

`overallScore` 不接受模型直接决定。Runtime 只在足够核心维度具备证据后确定性计算，最后再由代码渲染 Markdown，避免模型同时维护 JSON 与长 Markdown 两份结果。

## 7. JSON 不符合格式时，具体怎样修复？

### 7.1 发现错误

以下任意情况会令本轮 `decision=None`：

- function arguments 不是合法 JSON；
- 找不到完整 JSON object；
- Pydantic schema 校验失败；
- 模型调用终态函数却返回 `done=false` 且没有 output；
- Report 缺少必填字段、字段类型错误或语义校验失败。

Runtime 保留具体 `schema_error`，例如：

```text
provider function arguments are not valid JSON:
Expecting property name enclosed in double quotes: line 1 column 1500
```

### 7.2 闭合上一轮 Tool Calling 协议

如果错误来自终态 Function Call，Runtime 会先为原来的每个 `tool_call_id` 追加一条 Tool Response：

```json
{
  "success": false,
  "error": "具体 schema error",
  "retryable": true
}
```

这是 OpenAI-compatible Tool Calling 协议要求：Assistant 声明过的每个 tool_call_id 都必须有对应 Tool Response，否则 Provider 可能在修复请求到达前直接以 400 拒绝整段历史。

### 7.3 切换到独立 JSON-only 通道

修复轮不会再次暴露 Skill、MCP 或业务 Tool：

```text
tools = null
tool_choice = null
response_format = {"type": "json_object"}
```

Runtime 明确追加：

```text
上一次原生函数参数不是合法 JSON。
本轮不要调用任何工具，直接输出一个符合输出 schema 的 JSON 对象，
不要使用 markdown 或 DSML 包装。
```

同时把上一轮的具体 schema error 一并放入上下文。这样修复轮只有一个任务：重新提交完整结构化结果，不允许模型借机继续搜索或加载 Skill。

切换到 JSON mode 前，Runtime 会保留普通 system/user/assistant 内容，并移除历史中的 Tool 协议 frame，避免部分兼容网关因“历史有 tool_calls、当前却没有 tools”而返回 400。

当前实现需要特别注意：确定性 pre-step 的结果已经写进基础 user message 的 `[工具观察]`，因此仍会保留；但 ReAct 过程中只存在于原生 `role=tool` 消息里的 Skill/MCP/业务 Tool Result 会随协议 frame 一起被删除。已加载 Skill 的正文会重新进入 system context，部分 MCP 证据也可能已落入 SharedState，但不能据此宣称所有 Tool Observation 都保留。这是当前修复链路的真实缺口：正确做法应在删除协议 frame 前，将需要的成功 Tool Result 有界地扁平化为普通 `[此前工具观察]` 文本，同时排除终态 `emit_decision` 的错误回执。

### 7.4 修复轮重新走完整校验

修复输出不会因为名字叫“repair”就被直接接受，仍然重新经过：

```text
JSON object 提取
  → AgentDecision.model_validate
  → Report 额外语义校验
  → canonical AgentOutput
```

只有全部通过才算修复成功。

## 8. 修复次数和预算边界

完整评估中，每个 Agent 最多保留 3 个 decision iteration，用于正常终态输出和条件式修复。Action Round 与 decision iteration 分开计数，所以正常的：

```text
load_skill → Observation → 最终 decision
```

不会因为加载 Skill 就吃掉全部 JSON 修复空间。

如果 malformed final 正好耗尽了原计划轮数，但 Run 总 LLM 预算仍有余量，Runtime 可以：

- 最多借用 1 个修复 Round；
- 发出 `run.progress(stage=budget_reallocated)`；
- 记录原计划 quota、借用次数、有效轮数上限和 schemaError；
- 仍受 Run 总调用数、Token、成本和 Agent scope budget 的硬限制。

这不是无限重试。无剩余预算、超过 decision limit 或修复后仍不合法时，停止修复。

## 9. 输出过长或被截断时

Token 多不会自动新增 Agent Round，但可能间接导致 JSON 截断：

```text
finish_reason=length
    ↓
当前输出不可能形成闭合 JSON
    ↓
若未到 hard ceiling，Provider 调用以双倍输出额度重试一次
    ↓
仍超限则 JSON_TRUNCATED
    ↓
进入受预算约束的修复/失败路径
```

RiskAgent 和 ReportAgent 的修复输出有 hard token ceiling，避免“为了补 JSON 不断扩大输出”拖垮整个 Run。

Provider 的超时、429、5xx 属于传输/API 重试；JSON/schema 修复属于新的 Agent decision Round。两者必须分开统计：

- API 重试通常增加同一逻辑调用的耗时；
- JSON 修复会增加 Agent Round 和一次新的 LLM 调用。

## 10. 最终仍修不好怎么办？

### Specialist

一般 Specialist 抛出：

```text
MALFORMED_OUTPUT:
agent output failed schema validation within budget
```

Runtime 记录 `agent.failed`，merge/replan 根据缺失 artifact 决定是否还能继续。不会生成假的 claims/evidence 填坑。

### RiskAgent 的受限降级

RiskAgent 如果 JSON repair 仍然失败，可以保留已经由确定性工具生成的 timeline 结果，把有依据的风险综合交给 ReportAgent，并记录：

```text
stage=specialist_repair_compacted
```

它不会把未知事实写成候选人造假。

### ReportAgent

ReportAgent 是终态节点。若最终仍没有通过结构化报告校验，Run 不能把自由文本冒充正式报告，可能收敛为 `PARTIAL_SUCCESS`，并带：

```text
ReportAgent_failed
no_terminal_answer
```

## 11. 20 份真实压测结果

本轮 20 份简历共执行：

| 指标 | 结果 |
|---|---:|
| Run | 20 |
| 成功 | 20/20 |
| Agent 执行 | 80 |
| LLM Round | 160 |
| JSON/schema 修复 Round | 5 |
| 修复成功 | 5/5 |
| `agent.failed` | 0 |
| `llm.failed` | 0 |
| `llm.retrying` | 0 |

修复分布：

| Agent | 修复 Round |
|---|---:|
| ProjectAgent | 3 |
| RiskAgent | 1 |
| TechAgent | 1 |
| ReportAgent | 0 |

其中 4 次 malformed final 已经耗尽原计划轮数，因此各借用 1 个修复 Round；另外 1 次使用预留修复空间，无需借预算。

明确记录到的错误包括：

```text
Expecting ',' delimiter: line 1 column 2811
Expecting property name enclosed in double quotes: line 1 column 1500
Expecting ',' delimiter: line 1 column 3404
Expecting ',' delimiter: line 1 column 2998
```

这批数据说明：Function Schema 能降低错误率，但不能替代应用侧校验；真正有效的是“强约束生成 + 严格解析 + schema/语义校验 + 有界修复 + 明确失败”的闭环。

## 12. 面试时怎么讲

可以压缩为下面这段：

> 我没有只靠 Prompt 要求模型输出 JSON，而是做了四层保障。第一层用原生 Function Calling 和 JSON Schema 限制字段、枚举和长度，工具阶段结束后强制终态函数；第二层保留 raw arguments 并严格 JSON 解析，不做可能篡改语义的字符串补丁；第三层用 Pydantic 校验统一 AgentDecision，Report 再做必填字段、证据引用和评分语义校验；第四层在失败时关闭所有 Tool，携带精确错误进入 JSON-only 修复轮，最多借一个受总预算约束的修复调用。仍失败就显式 MALFORMED_OUTPUT 或受限降级，绝不把坏 JSON 写入 SharedState。20 份实测 160 个 Round 中出现 5 个修复 Round，最终 5/5 修复成功。

## 对应代码

- `workflow/app/runtime/executor.py`：Function Schema、decision loop、JSON repair、Report 语义校验
- `workflow/app/runtime/llm.py`：Provider JSON mode、raw arguments 解析、截断与 API 重试
- `workflow/app/runtime/models.py`：AgentDecision、StructuredReport、SourceRef 等 Pydantic 模型
- `reports/memory_react_load20_20260820/memory_react_metrics.json`：20 份实测原始聚合指标
