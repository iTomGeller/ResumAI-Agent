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
}>();

const emit = defineEmits<{
  revisionCreated: [response: ConversationTurnResponse];
  controlTurn: [response: ConversationTurnResponse];
  selectRevision: [traceId: string];
  statusChange: [response: TaskControlResponse];
}>();

const draft = ref('');
const queueMode = ref<'collect' | 'interrupt'>('collect');
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

const runStatusLabels: Record<string, string> = {
  QUEUED: '排队中',
  STARTING: '启动中',
  RUNNING: '分析中',
  WAITING_LLM: '模型生成中',
  WAITING_TOOL: '工具执行中',
  WAITING_SANDBOX: 'Sandbox 执行中',
  CANCELLING: '取消中',
  CANCELLED: '已取消',
  SUCCEEDED: '已完成',
  FAILED: '失败',
  TIMED_OUT: '已超时',
};

const runActive = computed(() => !!activeRun.value
  && !['SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'].includes(activeRun.value.status));
const runStatusLabel = computed(() => activeRun.value
  ? (runStatusLabels[activeRun.value.status] || activeRun.value.status) : '');
const lastFinishedRun = computed(() => activeRun.value
  && ['SUCCEEDED', 'FAILED', 'TIMED_OUT'].includes(activeRun.value.status)
  ? activeRun.value : null);

async function stopGeneration() {
  await cancelRun();
}

async function reanalyze() {
  const lastUser = [...messages.value].reverse().find((m) => m.role === 'USER');
  if (!lastUser) return;
  await sendMessage(`重新分析：${lastUser.content}`, props.revisionNo, 'interrupt');
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
  const response = await sendMessage(content, props.revisionNo, queueMode.value);
  if (!response) return;
  draft.value = '';
  queueMode.value = 'collect';
  if (response.action === 'REVISION_CREATED' && response.activeTraceId) {
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
</script>

<template>
  <aside class="conversation-panel" aria-label="候选人持续会话">
    <header class="conversation-panel-head">
      <div>
        <span class="conversation-eyebrow">持续会话</span>
        <h3>评估 Copilot</h3>
      </div>
      <button type="button" class="conversation-refresh" :disabled="loading" title="刷新会话" @click="loadConversation(conversationId || traceId)">↻</button>
    </header>

    <div class="conversation-run-state">
      <span class="conversation-status" :data-status="effectiveStatus">{{ statusLabel }}</span>
      <span>当前 v{{ activeRevision || revisionNo || 1 }}</span>
      <span v-if="!isViewedCurrent" class="conversation-view-warning">正在查看历史 revision</span>
    </div>

    <div class="conversation-controls" aria-label="任务控制">
      <button type="button" :disabled="!canPause || !!controlling" @click="requestControl('PAUSE')">
        {{ controlling === 'PAUSE' ? '请求中…' : '暂停' }}
      </button>
      <button type="button" :disabled="!canResume || !!controlling" @click="requestControl('RESUME')">
        {{ controlling === 'RESUME' ? '恢复中…' : '继续' }}
      </button>
      <button type="button" class="is-danger" :disabled="!canCancel || !!controlling" @click="requestControl('CANCEL')">
        {{ controlling === 'CANCEL' ? '取消中…' : '取消' }}
      </button>
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

    <div v-if="lastTurn?.answerThenResume" class="conversation-notice is-info">
      独立问题已处理，原评估继续运行，trace 未切换。
    </div>
    <div v-else-if="lastTurn?.action === 'REVISION_CREATED'" class="conversation-notice is-success">
      已创建 v{{ lastTurn.activeRevision }}；仅重跑受影响节点。
    </div>
    <div v-if="lastTurn?.needsConfirmation" class="conversation-notice is-warning">
      这条消息存在目标歧义，请在对话中明确是“只比较”还是“改为新目标重新评估”。
    </div>
    <div v-if="lastControl" class="conversation-notice" :class="['PAUSING', 'RESUMING'].includes(lastControl.status) ? 'is-warning' : 'is-info'">
      {{ lastControl.message }}
    </div>
    <div v-if="error" class="conversation-error" role="alert">{{ error }}</div>

    <form class="conversation-composer" @submit.prevent="submitMessage">
      <div v-if="runActive" class="queue-mode-picker">
        <label :class="{ active: queueMode === 'collect' }">
          <input v-model="queueMode" type="radio" value="collect" />
          补充信息（当前任务完成后合并执行）
        </label>
        <label :class="{ active: queueMode === 'interrupt' }">
          <input v-model="queueMode" type="radio" value="interrupt" />
          打断重来（取消当前任务）
        </label>
      </div>
      <textarea
        v-model="draft"
        rows="3"
        :disabled="sending"
        placeholder="问评估依据，或明确说“改投…重新评估”创建新 revision"
        @keydown="onComposerKeydown"
      />
      <div class="conversation-composer-foot">
        <span>Ctrl / ⌘ + Enter 发送</span>
        <div class="composer-actions">
          <button
            v-if="messages.length"
            type="button"
            class="composer-secondary"
            :disabled="sending"
            @click="reanalyze"
          >重新分析</button>
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
  overflow: hidden;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--shadow-md);
}

.conversation-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px;
}

.conversation-eyebrow {
  color: var(--color-primary);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.conversation-panel-head h3 { margin-top: 1px; font-size: 14px; }

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
  font-size: 10px;
}

.conversation-status {
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  font-weight: 600;
}

.conversation-status[data-status="RUNNING"],
.conversation-status[data-status="PAUSING"],
.conversation-status[data-status="RESUMING"] { background: var(--color-warning-light); color: var(--color-warning); }
.conversation-status[data-status="PAUSED"] { background: #e0e7ff; color: #4338ca; }
.conversation-status[data-status="SUCCESS"] { background: var(--color-success-light); color: var(--color-success); }
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
  font-size: 10px;
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
  font-size: 10px;
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
  font-size: 10px;
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
  font-size: 10px;
  font-weight: 600;
}
.run-error { color: var(--color-danger); }
.run-feedback button {
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  font-size: 10px;
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
  display: flex;
  gap: 8px;
  margin-bottom: 7px;
}
.queue-mode-picker label {
  flex: 1;
  padding: 5px 8px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  color: var(--color-text-secondary);
  font-size: 9px;
  cursor: pointer;
}
.queue-mode-picker label.active { border-color: var(--color-primary); color: var(--color-primary); }
.queue-mode-picker input { margin-right: 4px; }

.composer-actions { display: flex; gap: 6px; }
.composer-secondary {
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 10px;
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
  font-size: 11px;
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

.conversation-composer-foot span { color: var(--color-text-muted); font-size: 9px; }
.conversation-composer-foot button {
  padding: 6px 12px;
  border-radius: 7px;
  background: var(--color-primary);
  color: white;
  font-size: 10px;
  font-weight: 600;
}
</style>
