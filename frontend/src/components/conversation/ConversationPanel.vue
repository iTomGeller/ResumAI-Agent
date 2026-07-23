<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useConversation, type ConversationControlAction, type ConversationTurnResponse, type TaskControlResponse } from '../../composables/useConversation';
import ConversationMessageList from './ConversationMessageList.vue';
import RevisionSwitcher from './RevisionSwitcher.vue';

const props = defineProps<{
  conversationId: string;
  traceId: string;
  revisionNo?: number;
  taskStatus: string;
  overallScore?: number | null;
  recommendation?: string | null;
  runId?: string | null;
  extraContextRefs?: Array<{ type: string; id: string; revision?: number; version?: number }>;
  width?: number;
  fullscreen?: boolean;
}>();

const emit = defineEmits<{
  revisionCreated: [response: ConversationTurnResponse];
  controlTurn: [response: ConversationTurnResponse];
  selectRevision: [traceId: string];
  statusChange: [response: TaskControlResponse];
  'update:width': [width: number];
  'update:fullscreen': [fullscreen: boolean];
}>();

const panelWidth = computed(() => Math.min(720, Math.max(320, props.width ?? 420)));
const isFullscreen = computed(() => !!props.fullscreen);
const resizing = ref(false);

function toggleFullscreen() {
  emit('update:fullscreen', !isFullscreen.value);
}

function onResizeStart(event: PointerEvent) {
  if (isFullscreen.value) return;
  resizing.value = true;
  const startX = event.clientX;
  const startWidth = panelWidth.value;
  const onMove = (e: PointerEvent) => {
    const next = Math.min(720, Math.max(320, startWidth + (startX - e.clientX)));
    emit('update:width', next);
  };
  const onUp = () => {
    resizing.value = false;
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}

const draft = ref('');
const feedbackSent = ref<Record<string, boolean>>({});
const {
  activeTraceId,
  activeRevision,
  messages,
  revisions,
  loading,
  sending,
  controlling,
  error,
  lastTurn,
  lastControl,
  activeRun,
  runElapsedSeconds,
  loadConversation,
  sendMessage,
  controlTask,
  cancelRun,
  submitRunFeedback,
  reset,
} = useConversation();

const contextRefs = computed(() => {
  const refs: Array<{ type: string; id: string; revision?: number; version?: number }> = [];
  const seen = new Set<string>();
  const push = (ref: { type: string; id: string; revision?: number; version?: number }) => {
    if (!ref?.type || !ref?.id) return;
    const key = `${ref.type}:${ref.id}`;
    if (seen.has(key)) return;
    seen.add(key);
    refs.push(ref);
  };
  if (props.conversationId) {
    push({ type: 'candidate', id: props.conversationId, revision: props.revisionNo });
  }
  if (props.traceId) {
    push({ type: 'application', id: props.traceId });
  }
  if (props.runId) {
    push({ type: 'run', id: props.runId });
  }
  for (const ref of props.extraContextRefs || []) {
    push(ref);
  }
  return refs;
});

const dispositionLabels: Record<string, string> = {
  DIRECT_REPLY: '直接回复',
  BACKGROUND_QUERY: '证据查询',
  MERGE_CONTEXT: '已合并补充',
  CREATE_REVISION: '创建 revision',
  SUPERSEDE_RUN: '替换当前任务',
  CONTROL: '运行控制',
};

const runStatusLabels: Record<string, string> = {
  QUEUED: '排队中',
  STARTING: '启动中',
  RUNNING: '分析中',
  WAITING_LLM: '模型生成中',
  WAITING_TOOL: '工具执行中',
  WAITING_SANDBOX: '工具执行中',
  CANCELLING: '取消中',
  CANCELLED: '已取消',
  SUCCEEDED: '已完成',
  PARTIAL_SUCCESS: '部分完成',
  PAUSED: '已暂停',
  PAUSING: '暂停中',
  RESUMING: '恢复中',
  FAILED: '失败',
  TIMED_OUT: '已超时',
};

const runActive = computed(() => !!activeRun.value
  && !['SUCCEEDED', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED', 'TIMED_OUT'].includes(activeRun.value.status));
const runStatusLabel = computed(() => activeRun.value
  ? (runStatusLabels[activeRun.value.status] || activeRun.value.status) : '');
const lastFinishedRun = computed(() => activeRun.value
  && ['SUCCEEDED', 'PARTIAL_SUCCESS', 'FAILED', 'TIMED_OUT'].includes(activeRun.value.status)
  ? activeRun.value : null);

async function stopGeneration() {
  await cancelRun();
}

const retrying = ref(false);
const retryError = ref('');

/** 失败 run 的断点重试：只重跑失败的那一段，完成的 Agent 不再执行。 */
async function retryFromCheckpoint() {
  const runId = lastFinishedRun.value?.runId || props.runId;
  if (!runId || retrying.value) return;
  retrying.value = true;
  retryError.value = '';
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/retry`, { method: 'POST' });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      retryError.value = body.message || `重试失败（HTTP ${response.status}）`;
      return;
    }
    await loadConversation(props.conversationId || props.traceId);
  } catch (e) {
    retryError.value = '重试请求失败，请稍后再试';
  } finally {
    retrying.value = false;
  }
}

const canRetry = computed(() => {
  const status = lastFinishedRun.value?.status || effectiveStatus.value;
  return ['FAILED', 'TIMED_OUT'].includes(status);
});

const recommendationText = computed(() => {
  switch (props.recommendation) {
    case 'HIRE': return '建议录用';
    case 'INTERVIEW_RECOMMEND':
    case 'RECOMMEND':
    case 'STRONG_RECOMMEND': return '推荐面试';
    case 'NOT_RECOMMEND': return '不推荐';
    default: return props.recommendation ? '需人工复核' : '';
  }
});

// ---------- plan-approval mode ----------
const AGENT_LABELS: Record<string, string> = {
  ResumeParserAgent: '简历解析', JDAnalysisAgent: 'JD 分析', TechAgent: '技术评估',
  ProjectAgent: '项目分析', RiskAgent: '风险审查', EvidenceAgent: '证据核验',
  ReportAgent: '报告生成', ResumeOptimizeAgent: '简历优化', InterviewQuestionAgent: '面试追问',
};
const awaitingPlan = computed(() => !!activeRun.value?.awaitingPlanApproval
  || (effectiveStatus.value === 'PAUSED' && !!activeRun.value?.plannedPipeline?.length
      && activeRun.value?.awaitingPlanApproval !== false));
const planDraft = ref<Array<{ name: string; enabled: boolean }>>([]);
watch(() => activeRun.value?.plannedPipeline, (pipeline) => {
  if (pipeline?.length) {
    planDraft.value = pipeline.map((name) => ({ name, enabled: true }));
  }
}, { immediate: true });

const approving = ref(false);
async function approvePlan() {
  if (approving.value) return;
  approving.value = true;
  try {
    const approved = planDraft.value.filter((a) => a.enabled).map((a) => a.name);
    const response = await controlTask(props.traceId, 'RESUME',
      approved.length ? approved : undefined);
    if (response) emit('statusChange', response);
  } finally {
    approving.value = false;
  }
}

async function sendRunFeedback(runId: string, positive: boolean) {
  const result = await submitRunFeedback(runId, {
    ratingScore: positive ? 5 : 2,
    accepted: positive,
    recommendationAgreed: positive,
    comment: positive ? '结果可用' : '结果需要改进',
  });
  if (result) feedbackSent.value = { ...feedbackSent.value, [runId]: true };
}

const statusLabels: Record<string, string> = {
  QUEUED: '排队中',
  RETRYING: '待重试',
  RUNNING: '评估中',
  PAUSING: '安全暂停中',
  RESUMING: '从 checkpoint 恢复中',
  PAUSED: '已暂停',
  SUCCESS: '已完成',
  PARTIAL_SUCCESS: '部分完成',
  FAILED: '失败',
  CANCELLED: '已取消',
  SUPERSEDED: '已被新 revision 替代',
};

const viewedRevision = computed(() => revisions.value.find((revision) => revision.traceId === props.traceId));
const effectiveStatus = computed(() => props.taskStatus || viewedRevision.value?.status || '');
const displayRevisions = computed(() => revisions.value.map((revision) => (
  revision.traceId === props.traceId && props.taskStatus
    ? { ...revision, status: props.taskStatus }
    : revision
)));
const isViewedCurrent = computed(() => !activeTraceId.value || activeTraceId.value === props.traceId);
const isTerminal = computed(() => ['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED', 'SUPERSEDED'].includes(effectiveStatus.value));
const canPause = computed(() => ['RUNNING', 'QUEUED', 'RETRYING'].includes(effectiveStatus.value) && isViewedCurrent.value);
const canResume = computed(() => effectiveStatus.value === 'PAUSED' && isViewedCurrent.value);
const canCancel = computed(() => ['RUNNING', 'QUEUED', 'RETRYING', 'PAUSING', 'PAUSED', 'RESUMING'].includes(effectiveStatus.value) && !isTerminal.value && isViewedCurrent.value);
const statusLabel = computed(() => statusLabels[effectiveStatus.value] || effectiveStatus.value || '状态未知');

watch(() => props.conversationId || props.traceId, (id) => {
  if (id) void loadConversation(id);
  else reset();
}, { immediate: true });

async function submitMessage() {
  const content = draft.value.trim();
  if (!content) return;
  const response = await sendMessage(content, props.revisionNo, contextRefs.value);
  if (!response) return;
  draft.value = '';
  if ((response.action === 'REVISION_CREATED' || response.action === 'RUN_SUPERSEDED')
      && response.activeTraceId) {
    emit('revisionCreated', response);
  } else if (['PAUSE', 'RESUME', 'CANCEL'].includes(response.action)) {
    emit('controlTurn', response);
  }
}

function onComposerKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    void submitMessage();
  }
}

async function requestControl(action: ConversationControlAction) {
  if (action === 'CANCEL' && !window.confirm('确定立即取消当前 revision？迟到结果不会覆盖有效版本。')) return;
  const response = await controlTask(props.traceId, action);
  if (response) emit('statusChange', response);
}

function friendlyError(raw: string): string {
  if (/不支持\s*PAUSE/.test(raw)) return '任务已经结束，无需暂停；可以直接继续追问或重新分析。';
  if (/不支持\s*RESUME/.test(raw)) return '任务不在暂停状态，无需恢复。';
  if (/不支持\s*CANCEL|任务已取消/.test(raw)) return '任务已经结束，无需取消。';
  if (/并发操作/.test(raw)) return '状态刚被其他操作更新，已自动刷新，请重试。';
  return raw;
}
</script>

<template>
  <aside
    class="conversation-panel"
    :class="{ fullscreen: isFullscreen, resizing }"
    :style="isFullscreen ? undefined : { width: panelWidth + 'px' }"
    aria-label="系统 Copilot"
  >
    <div
      v-if="!isFullscreen"
      class="conversation-resizer"
      title="拖动调整宽度"
      @pointerdown.prevent="onResizeStart"
    ></div>
    <header class="conversation-panel-head">
      <div>
        <span class="conversation-eyebrow">系统 Copilot</span>
        <h3>评估助手</h3>
      </div>
      <div class="conversation-head-actions">
        <button type="button" class="conversation-refresh" :title="isFullscreen ? '退出全屏' : '全屏'" @click="toggleFullscreen">
          {{ isFullscreen ? '⧉' : '⛶' }}
        </button>
        <button type="button" class="conversation-refresh" :disabled="loading" title="刷新会话" @click="loadConversation(conversationId || traceId)">↻</button>
      </div>
    </header>

    <div class="conversation-run-state">
      <span class="conversation-status" :data-status="effectiveStatus">{{ statusLabel }}</span>
      <span>当前 v{{ activeRevision || revisionNo || 1 }}</span>
      <span v-if="!isViewedCurrent" class="conversation-view-warning">正在查看历史 revision</span>
    </div>

    <!-- Plan 确认卡：Coordinator 规划后暂停，确认（可勾选删减）后才开始消耗预算 -->
    <div v-if="awaitingPlan && planDraft.length" class="plan-approval-card">
      <div class="plan-approval-head">
        <strong>执行计划待确认</strong>
        <span>Coordinator 已规划评估流水线，未开始消耗预算</span>
      </div>
      <div class="plan-approval-list">
        <label v-for="step in planDraft" :key="step.name" class="plan-step" :class="{ off: !step.enabled }">
          <input v-model="step.enabled" type="checkbox" :disabled="step.name === 'ReportAgent'" />
          <span>{{ AGENT_LABELS[step.name] || step.name }}</span>
        </label>
      </div>
      <div class="plan-approval-actions">
        <button type="button" class="plan-approve" :disabled="approving" @click="approvePlan">
          {{ approving ? '启动中…' : '✓ 确认执行' }}
        </button>
        <span class="plan-tip">去掉勾选可跳过对应 Agent（报告生成不可跳过）</span>
      </div>
    </div>

    <div v-else-if="canPause || canResume || canCancel" class="conversation-controls" aria-label="任务控制">
      <button v-if="canPause" type="button" :disabled="!!controlling" @click="requestControl('PAUSE')">
        {{ controlling === 'PAUSE' ? '请求中…' : '⏸ 暂停' }}
      </button>
      <button v-if="canResume" type="button" :disabled="!!controlling" @click="requestControl('RESUME')">
        {{ controlling === 'RESUME' ? '恢复中…' : '▶ 继续' }}
      </button>
      <button v-if="canCancel" type="button" class="is-danger" :disabled="!!controlling" @click="requestControl('CANCEL')">
        {{ controlling === 'CANCEL' ? '取消中…' : '✕ 取消' }}
      </button>
    </div>
    <div v-else-if="isTerminal" class="conversation-result-card">
      <div class="result-line">
        <span class="result-score" v-if="overallScore">{{ overallScore }}<em>分</em></span>
        <span class="result-recommendation" v-if="recommendationText">{{ recommendationText }}</span>
        <span class="result-hint" v-if="!overallScore && !recommendationText">本轮评估已结束</span>
      </div>
      <div class="result-actions">
        <button v-if="canRetry" type="button" class="result-retry" :disabled="retrying" @click="retryFromCheckpoint">
          {{ retrying ? '重试中…' : '⟲ 从断点重试' }}
        </button>
        <span class="result-tip">可继续追问，或点下方“重新分析”发起新一轮</span>
      </div>
      <div v-if="retryError" class="conversation-error" role="alert">{{ retryError }}</div>
    </div>

    <div v-if="activeRun" class="run-monitor" :data-active="runActive">
      <div class="run-monitor-row">
        <span class="conversation-status" :data-status="activeRun.status">{{ runStatusLabel }}</span>
        <span v-if="activeRun.queuePosition && activeRun.status === 'QUEUED'">队列第 {{ activeRun.queuePosition }} 位</span>
        <span v-if="runActive" class="run-elapsed">{{ runElapsedSeconds }}s</span>
        <button
          v-if="runActive"
          type="button"
          class="run-stop"
          @click="stopGeneration"
        >停止生成</button>
      </div>
      <div v-if="runActive" class="run-monitor-row run-detail">
        <span v-if="activeRun.currentAgent">Agent: {{ activeRun.currentAgent }}</span>
        <span v-if="activeRun.currentTool">工具: {{ activeRun.currentTool }}</span>
        <span v-if="activeRun.llmActive" class="run-llm">● LLM 调用中</span>
        <span v-if="activeRun.retrying" class="run-retry">重试中…</span>
        <span v-if="activeRun.policyId">策略: {{ activeRun.policyId }}</span>
      </div>
      <div v-if="activeRun.errorMessage" class="run-monitor-row run-error">
        {{ activeRun.errorCode }} {{ activeRun.errorMessage }}
      </div>
      <div v-if="lastFinishedRun && !feedbackSent[lastFinishedRun.runId]" class="run-monitor-row run-feedback">
        <span>这次结果有帮助吗？</span>
        <button type="button" @click="sendRunFeedback(lastFinishedRun.runId, true)">👍 有用</button>
        <button type="button" @click="sendRunFeedback(lastFinishedRun.runId, false)">👎 不准</button>
      </div>
      <div v-else-if="lastFinishedRun && feedbackSent[lastFinishedRun.runId]" class="run-monitor-row run-feedback">
        <span>反馈已记录，将用于策略学习。</span>
      </div>
    </div>

    <RevisionSwitcher
      :revisions="displayRevisions"
      :active-trace-id="activeTraceId"
      :viewed-trace-id="traceId"
      @select="emit('selectRevision', $event)"
    />

    <ConversationMessageList :messages="messages" :loading="loading" />

    <div v-if="lastTurn?.disposition" class="conversation-notice is-info">
      {{ dispositionLabels[lastTurn.disposition] || lastTurn.disposition }}
      <span v-if="lastTurn.reason"> · {{ lastTurn.reason }}</span>
    </div>
    <div v-if="lastTurn?.answerThenResume" class="conversation-notice is-info">
      独立问题已处理，原评估继续运行，trace 未切换。
    </div>
    <div v-else-if="lastTurn?.action === 'REVISION_CREATED' || lastTurn?.action === 'RUN_SUPERSEDED'" class="conversation-notice is-success">
      已创建 v{{ lastTurn.activeRevision }}；系统已自动处理，无需选择打断模式。
    </div>
    <div v-if="lastTurn?.needsConfirmation" class="conversation-notice is-warning">
      这条消息存在目标歧义，请在对话中明确是“只比较”还是“改为新目标重新评估”。
    </div>
    <div v-if="lastControl" class="conversation-notice" :class="['PAUSING', 'RESUMING'].includes(lastControl.status) ? 'is-warning' : 'is-info'">
      {{ lastControl.message }}
    </div>
    <div v-if="error" class="conversation-error" role="alert">{{ friendlyError(error) }}</div>

    <form class="conversation-composer" @submit.prevent="submitMessage">
      <div v-if="contextRefs.length" class="context-chip-list" aria-label="当前上下文">
        <span v-for="ref in contextRefs" :key="`${ref.type}:${ref.id}`" class="context-chip">
          {{ ref.type }}{{ ref.revision != null ? ` v${ref.revision}` : '' }}
        </span>
      </div>
      <textarea
        v-model="draft"
        rows="3"
        :disabled="sending"
        placeholder="问评估依据或闲聊；改岗位/补事实系统会自动处理。停止请点下方按钮或说“停止”"
        @keydown="onComposerKeydown"
      />
      <div class="conversation-composer-foot">
        <span>Ctrl / ⌘ + Enter 发送</span>
        <div class="composer-actions">
          <button
            v-if="runActive"
            type="button"
            class="run-stop"
            @click="stopGeneration"
          >停止生成</button>
          <button type="submit" :disabled="sending || !draft.trim()">{{ sending ? '发送中…' : '发送' }}</button>
        </div>
      </div>
    </form>
  </aside>
</template>

<style scoped>
.conversation-panel {
  position: sticky;
  top: 18px;
  align-self: start;
  width: 420px;
  max-width: 100%;
  overflow: hidden;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-md);
  font-size: 14px;
  line-height: 1.55;
}

.conversation-panel.fullscreen {
  position: fixed;
  inset: 12px;
  z-index: 1200;
  width: auto !important;
  max-width: none;
  border-radius: 14px;
}

.conversation-resizer {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 6px;
  cursor: col-resize;
  z-index: 2;
}
.conversation-resizer:hover,
.conversation-panel.resizing .conversation-resizer {
  background: rgba(37, 99, 235, 0.25);
}

.conversation-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px;
}

.conversation-head-actions { display: flex; gap: 6px; }

.conversation-eyebrow {
  color: var(--color-primary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.conversation-panel-head h3 { margin-top: 1px; font-size: 16px; }

.conversation-refresh {
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.conversation-run-state {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 14px 10px;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.conversation-status {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  font-weight: 600;
  font-size: 12px;
}

.conversation-status[data-status="RUNNING"],
.conversation-status[data-status="PAUSING"],
.conversation-status[data-status="RESUMING"] { background: var(--color-warning-light); color: var(--color-warning); }
.conversation-status[data-status="PAUSED"] { background: #e0e7ff; color: #4338ca; }
.conversation-status[data-status="SUCCESS"],
.conversation-status[data-status="PARTIAL_SUCCESS"] { background: var(--color-success-light); color: var(--color-success); }

.conversation-result-card {
  margin: 0 14px 10px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--color-border-light);
  background: var(--color-bg);
}
.result-line { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.result-score { font-size: 22px; font-weight: 700; color: var(--color-primary); }
.result-score em { font-style: normal; font-size: 12px; color: var(--color-text-secondary); margin-left: 2px; }
.result-recommendation {
  padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600;
  background: var(--color-success-light); color: var(--color-success);
}
.result-hint { font-size: 14px; color: var(--color-text-secondary); }
.result-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.result-retry {
  border: 1px solid var(--color-primary); background: none; color: var(--color-primary);
  border-radius: 8px; padding: 4px 10px; font-size: 13px; cursor: pointer;
}
.result-retry:disabled { opacity: .6; cursor: default; }
.result-tip { font-size: 12px; color: var(--color-text-secondary); }

/* Plan 确认卡 */
.plan-approval-card {
  margin: 0 14px 10px;
  padding: 12px;
  border-radius: 10px;
  border: 1px solid var(--color-primary);
  background: var(--color-primary-light, #eef2ff);
}
.plan-approval-head { display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
.plan-approval-head strong { font-size: 14px; }
.plan-approval-head span { font-size: 12px; color: var(--color-text-secondary); }
.plan-approval-list { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.plan-step {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 8px; border-radius: 999px; border: 1px solid var(--color-border);
  background: var(--color-surface); font-size: 13px; cursor: pointer;
}
.plan-step.off { opacity: .45; text-decoration: line-through; }
.plan-step input { margin: 0; }
.plan-approval-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.plan-approve {
  border: none; border-radius: 8px; padding: 6px 14px;
  background: var(--color-primary); color: #fff; font-size: 13px; font-weight: 600; cursor: pointer;
}
.plan-approve:disabled { opacity: .6; cursor: default; }
.plan-tip { font-size: 12px; color: var(--color-text-secondary); }
.conversation-status[data-status="CANCELLED"],
.conversation-status[data-status="FAILED"] { background: var(--color-danger-light); color: var(--color-danger); }
.conversation-status[data-status="SUPERSEDED"] { background: #e2e8f0; color: #475569; }

.conversation-view-warning { margin-left: auto; color: var(--color-warning); }

.conversation-controls {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  padding: 0 14px 10px;
}

.conversation-controls button {
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.conversation-controls button:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.conversation-controls button.is-danger:hover:not(:disabled) { border-color: var(--color-danger); color: var(--color-danger); }

.conversation-notice,
.conversation-error {
  margin: 9px 14px 0;
  padding: 7px 9px;
  border-radius: 7px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.5;
}

.conversation-notice.is-info { background: #eff6ff; color: #1d4ed8; }
.conversation-notice.is-success { background: var(--color-success-light); color: var(--color-success); }
.conversation-notice.is-warning { background: var(--color-warning-light); color: var(--color-warning); }
.conversation-error { background: var(--color-danger-light); color: var(--color-danger); }

.run-monitor {
  margin: 0 14px 10px;
  padding: 9px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  font-size: 13px;
}
.run-monitor[data-active="true"] { border-color: var(--color-primary); }
.run-monitor-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary);
}
.run-monitor-row + .run-monitor-row { margin-top: 6px; }
.run-detail span { padding: 1px 6px; border-radius: 6px; background: var(--color-surface); }
.run-llm { color: var(--color-primary); animation: pulse 1.4s infinite; }
.run-retry { color: var(--color-warning); }
.run-elapsed { margin-left: auto; font-variant-numeric: tabular-nums; }
.run-stop {
  padding: 3px 10px;
  border: 1px solid var(--color-danger);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-danger);
  font-size: 12px;
  font-weight: 600;
}
.run-error { color: var(--color-danger); }
.run-feedback button {
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  font-size: 12px;
}
.conversation-status[data-status="WAITING_LLM"],
.conversation-status[data-status="WAITING_TOOL"],
.conversation-status[data-status="WAITING_SANDBOX"],
.conversation-status[data-status="STARTING"],
.conversation-status[data-status="CANCELLING"] { background: var(--color-warning-light); color: var(--color-warning); }
.conversation-status[data-status="SUCCEEDED"] { background: var(--color-success-light); color: var(--color-success); }
.conversation-status[data-status="TIMED_OUT"] { background: var(--color-danger-light); color: var(--color-danger); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

.queue-mode-picker {
  display: none;
}
.context-chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 7px;
}
.context-chip {
  padding: 2px 8px;
  border-radius: 999px;
  background: #e2e8f0;
  color: #475569;
  font-size: 11px;
  font-weight: 600;
}

.composer-actions { display: flex; gap: 6px; }
.composer-secondary {
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 600;
}

.conversation-composer { padding: 12px 14px 14px; }
.conversation-composer textarea {
  width: 100%;
  resize: vertical;
  min-height: 70px;
  padding: 9px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  font-size: 14px;
  line-height: 1.5;
}
.conversation-composer textarea:focus { border-color: var(--color-primary); box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.08); }

.conversation-composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 7px;
}

.conversation-composer-foot span { color: var(--color-text-muted); font-size: 12px; }
.conversation-composer-foot button {
  padding: 6px 12px;
  border-radius: 7px;
  background: var(--color-primary);
  color: white;
  font-size: 13px;
  font-weight: 600;
}
</style>
