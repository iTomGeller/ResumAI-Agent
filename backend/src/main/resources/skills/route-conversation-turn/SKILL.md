---
name: route-conversation-turn
description: 路由持续简历评估对话中的每条用户消息。用户补充或纠正事实、增加约束、更换简历或 JD、临时询问其他问题、切换目标，以及要求暂停、取消、恢复或继续时使用；在触发任何评估工作流动作前调用。
---

# Route Conversation Turn

在不丢失当前任务的前提下解释本轮意图。先读取当前消息，再参考对话状态；最新的明确用户表述优先于旧状态。

## 输入

接收以下字段；缺失字段使用空值，不自行编造：

- `currentMessage`：当前用户原文。
- `conversationSummary`：仅包含已确认事实与决定的摘要。
- `activeGoal`：当前主目标及其状态。
- `pendingQuestion`：等待用户回答的问题。
- `workflowStatus`：`idle | running | paused | completed | failed | cancelled`。
- `artifactRevisions`：简历、JD、偏好和报告的当前 revision。

## 判定顺序

按以下优先级处理，一轮可同时包含一个主意图和若干附加动作：

1. 识别显式运行控制：暂停、取消、恢复、继续。
2. 识别事实纠正，例如“刚才项目人数说错了”。
3. 识别输入或目标变化，例如换 JD、换岗位、增加评估重点。
4. 识别临时岔题。能独立回答且不改变主任务时选择 `answer_then_resume`。
5. 识别对现有结果的解释或质疑，路由到 `explain-evaluation-decision`。
6. 仅在不同解释会造成不同写入、取消或大范围重跑时请求确认。

## 意图与动作

使用以下受控值：

- `primaryIntent`：`evaluate | edit_resume | compare_jobs | interview_prep | explain_result | provide_evidence | control | side_question | unknown`。
- `goalMutation`：`none | add_constraint | replace_goal | branch_goal | correct_fact`。
- `controlAction`：`none | pause | cancel | resume | continue | answer_then_resume`。

处理“突然有其他想法”时：

- 新想法只是问题：回答后恢复原任务，不清空 checkpoint。
- 新想法补充约束：保留目标，标记 `add_constraint`，交给 revision planner。
- 新想法替换目标：标记 `replace_goal`，保留旧 revision 历史。
- 用户明确想并行探索：标记 `branch_goal`，不要覆盖主目标。
- 用户没有说明是否替换且两种处理成本差异很大：设置 `needsConfirmation=true`。

## 输出

只输出紧凑 JSON：

```json
{
  "primaryIntent": "side_question",
  "dialogueAct": "ask_then_return",
  "goalMutation": "none",
  "controlAction": "answer_then_resume",
  "answerThenResume": true,
  "targetSkill": "explain-evaluation-decision",
  "affectedArtifacts": [],
  "needsConfirmation": false,
  "clarificationQuestion": null,
  "reason": "用户提出独立问题，没有要求替换当前评估目标",
  "sourceRefs": ["currentMessage"]
}
```

## 边界

- 不把抱怨、犹豫或负面情绪推断为取消。
- 不把临时岔题默认为新主目标。
- 不在路由阶段评价候选人，也不把路由结论写入候选人证据。
- 不静默丢弃旧目标、旧报告或 checkpoint。
- 对不可逆或高成本歧义只问一个最关键问题；其余情况继续安全的只读工作。
