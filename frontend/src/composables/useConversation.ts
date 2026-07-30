import { computed, onScopeDispose, ref } from 'vue';

export type ConversationControlAction = 'PAUSE' | 'RESUME' | 'CANCEL';

export interface ConversationMessage {
  id: number;
  clientMessageId: string;
  role: 'USER' | 'ASSISTANT' | string;
  intent?: string;
  content: string;
  revision: number;
  createdAt: string;
}

export interface ConversationRevision {
  traceId: string;
  revision: number;
  status: string;
  workflowRunId?: string;
  supersedesTraceId?: string;
  supersededByTraceId?: string;
  evaluationBrief?: string;
  createdAt?: string;
}

export interface ConversationSnapshot {
  conversationId: string;
  activeTraceId: string;
  activeRevision: number;
  messages: ConversationMessage[];
  revisions: ConversationRevision[];
}

export interface ConversationTurnResponse {
  conversationId: string;
  clientMessageId: string;
  intent: string;
  affectsEvaluation: boolean;
  answerThenResume: boolean;
  needsConfirmation: boolean;
  action: string;
  assistantMessage: string;
  activeTraceId: string;
  activeRevision: number;
  supersededTraceId?: string;
  affectedNodes: string[];
  runId?: string | null;
  runStatus?: string | null;
  queuePosition?: number | null;
  interruptedRunId?: string | null;
  disposition?: string | null;
  reason?: string | null;
  turnId?: string | null;
  citations?: Array<Record<string, unknown>>;
  actions?: Array<Record<string, unknown>>;
  suggestions?: string[];
}

export type ContextRef =
  | { type: 'candidate'; id: string; revision?: number }
  | { type: 'application'; id: string }
  | { type: 'job'; id: string; version?: number }
  | { type: 'knowledge_document'; id: string; version?: number }
  | { type: 'run'; id: string }
  | { type: string; id: string; revision?: number; version?: number };

export interface RunEventPayload {
  runId: string;
  conversationId: string;
  seq: number;
  eventType: string;
  agentId?: string;
  toolName?: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface RunView {
  runId: string;
  status: string;
  runType?: string;
  queuePosition?: number;
  currentAgent?: string;
  currentTool?: string;
  currentPhase?: string;
  answer?: string;
  errorCode?: string;
  errorMessage?: string;
  startedAt?: string;
  createdAt?: string;
  llmActive?: boolean;
  retrying?: boolean;
  lastEvent?: string;
  /** plan-approval mode: Coordinator paused with this plan awaiting approval */
  awaitingPlanApproval?: boolean;
  plannedPipeline?: string[];
  events: RunEventPayload[];
}

export interface TaskControlResponse {
  traceId: string;
  workflowRunId: string;
  action: ConversationControlAction;
  status: string;
  message: string;
}

async function responseError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { message?: string; error?: string; detail?: string };
    return body.message || body.detail || body.error || `请求失败（HTTP ${response.status}）`;
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}

function clientMessageId(): string {
  const randomId = globalThis.crypto?.randomUUID?.();
  return randomId ? `web-${randomId}` : `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function useConversation() {
  const conversationId = ref('');
  const activeTraceId = ref('');
  const activeRevision = ref(0);
  const messages = ref<ConversationMessage[]>([]);
  const revisions = ref<ConversationRevision[]>([]);
  const loading = ref(false);
  const sending = ref(false);
  const controlling = ref<ConversationControlAction | ''>('');
  const error = ref('');
  const lastTurn = ref<ConversationTurnResponse | null>(null);
  const lastControl = ref<TaskControlResponse | null>(null);

  let requestSequence = 0;
  let loadController: AbortController | null = null;

  const sortedRevisions = computed(() => [...revisions.value].sort((a, b) => b.revision - a.revision));

  function applySnapshot(snapshot: ConversationSnapshot) {
    conversationId.value = snapshot.conversationId;
    activeTraceId.value = snapshot.activeTraceId;
    activeRevision.value = snapshot.activeRevision;
    messages.value = snapshot.messages ?? [];
    revisions.value = snapshot.revisions ?? [];
  }

  function reset() {
    requestSequence += 1;
    loadController?.abort();
    loadController = null;
    conversationId.value = '';
    activeTraceId.value = '';
    activeRevision.value = 0;
    messages.value = [];
    revisions.value = [];
    error.value = '';
    lastTurn.value = null;
    lastControl.value = null;
  }

  async function loadConversation(id: string, silent = false): Promise<ConversationSnapshot | null> {
    const requestedId = id.trim();
    if (!requestedId) {
      reset();
      return null;
    }

    const sequence = ++requestSequence;
    loadController?.abort();
    loadController = new AbortController();
    if (!silent) loading.value = true;
    error.value = '';

    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(requestedId)}`, {
        signal: loadController.signal,
      });
      if (!response.ok) throw new Error(await responseError(response));
      const snapshot = await response.json() as ConversationSnapshot;
      if (sequence !== requestSequence) return null;
      applySnapshot(snapshot);
      return snapshot;
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === 'AbortError') return null;
      if (sequence === requestSequence) {
        error.value = caught instanceof Error ? caught.message : '会话加载失败';
      }
      return null;
    } finally {
      if (sequence === requestSequence) loading.value = false;
    }
  }

  // ---------------- run tracking (SSE with replay) ----------------

  const activeRun = ref<RunView | null>(null);
  const runHistory = ref<RunView[]>([]);
  let runSource: EventSource | null = null;
  let runTimer: ReturnType<typeof setInterval> | null = null;
  const runElapsedSeconds = ref(0);

  const TERMINAL_RUN = new Set(['SUCCEEDED', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED', 'TIMED_OUT']);

  function closeRunStream() {
    runSource?.close();
    runSource = null;
    if (runTimer) {
      clearInterval(runTimer);
      runTimer = null;
    }
  }

  function watchRun(runId: string) {
    closeRunStream();
    activeRun.value = { runId, status: 'QUEUED', events: [] };
    runElapsedSeconds.value = 0;
    const startedAt = Date.now();
    runTimer = setInterval(() => {
      runElapsedSeconds.value = Math.floor((Date.now() - startedAt) / 1000);
    }, 1000);
    // EventSource auto-reconnects and resends Last-Event-ID (runId:seq),
    // so the backend replays missed run_event rows after a disconnect.
    runSource = new EventSource(`/sse/runs/${encodeURIComponent(runId)}?afterSeq=0`);
    runSource.addEventListener('run', (raw) => {
      try {
        const event = JSON.parse((raw as MessageEvent).data) as RunEventPayload;
        applyRunEvent(event);
      } catch {
        /* malformed frame ignored */
      }
    });
    runSource.onerror = () => {
      /* EventSource retries automatically; state is rebuilt from replay */
    };
  }

  function applyRunEvent(event: RunEventPayload) {
    const run = activeRun.value && activeRun.value.runId === event.runId
      ? activeRun.value
      : { runId: event.runId, status: 'QUEUED', events: [] } as RunView;
    run.events = [...run.events.slice(-199), event];
    run.lastEvent = event.eventType;
    const payload = event.payload || {};
    if (event.agentId) run.currentAgent = event.agentId;
    if (event.toolName) run.currentTool = event.toolName;
    switch (event.eventType) {
      case 'run.queued':
        run.status = 'QUEUED';
        run.queuePosition = Number(payload.queuePosition ?? 0) || undefined;
        break;
      case 'run.started':
        run.status = 'RUNNING';
        run.queuePosition = undefined;
        break;
      case 'llm.started':
        run.status = 'WAITING_LLM';
        run.llmActive = true;
        run.retrying = false;
        break;
      case 'llm.retrying':
        run.retrying = true;
        break;
      case 'llm.completed':
      case 'llm.failed':
        run.llmActive = false;
        run.status = 'RUNNING';
        break;
      case 'tool.started':
        run.status = 'WAITING_TOOL';
        break;
      case 'tool.completed':
      case 'tool.failed':
        run.status = 'RUNNING';
        run.currentTool = undefined;
        break;
      case 'run.cancelling':
        run.status = 'CANCELLING';
        break;
      case 'agent.selected':
        run.plannedPipeline = Array.isArray(payload.plan)
          ? (payload.plan as string[]) : run.plannedPipeline;
        break;
      case 'run.progress':
        if (payload.stage === 'awaiting_plan_approval') {
          run.status = 'PAUSED';
          run.awaitingPlanApproval = true;
          run.plannedPipeline = Array.isArray(payload.plan)
            ? (payload.plan as string[]) : run.plannedPipeline;
        } else if (payload.stage === 'resumed') {
          run.awaitingPlanApproval = false;
          run.status = 'RUNNING';
        }
        break;
      case 'run.completed':
        run.status = 'SUCCEEDED';
        run.answer = String(payload.answer ?? '');
        break;
      case 'run.cancelled':
        run.status = 'CANCELLED';
        break;
      case 'run.timed_out':
        run.status = 'TIMED_OUT';
        run.errorMessage = String(payload.errorMessage ?? '运行超时');
        break;
      case 'run.failed':
        run.status = 'FAILED';
        run.errorCode = String(payload.errorCode ?? '');
        run.errorMessage = String(payload.errorMessage ?? '');
        break;
      default:
        break;
    }
    activeRun.value = { ...run };
    if (TERMINAL_RUN.has(run.status)) {
      runHistory.value = [{ ...run }, ...runHistory.value].slice(0, 20);
      closeRunStream();
      void loadConversation(conversationId.value, true);
    }
  }

  async function cancelRun(runId?: string): Promise<boolean> {
    const target = runId || activeRun.value?.runId;
    if (!target) return false;
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(target)}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: '用户点击停止生成' }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      if (activeRun.value && activeRun.value.runId === target) {
        activeRun.value = { ...activeRun.value, status: 'CANCELLING' };
      }
      return true;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '取消失败';
      return false;
    }
  }

  async function createConversation(payload: {
    title?: string; resumeText: string; jobDescription?: string; jobCategory?: string;
  }): Promise<string | null> {
    try {
      const response = await fetch('/api/conversations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const body = await response.json() as { conversationId: string };
      return body.conversationId;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '会话创建失败';
      return null;
    }
  }

  async function sendMessage(content: string, expectedRevision?: number,
                             contextRefs: ContextRef[] = []): Promise<ConversationTurnResponse | null> {
    const trimmed = content.trim();
    if (!trimmed || !conversationId.value || sending.value) return null;

    const messageId = clientMessageId();
    const optimisticId = -Date.now();
    const revision = activeRevision.value || expectedRevision || 1;
    messages.value.push({
      id: optimisticId,
      clientMessageId: messageId,
      role: 'USER',
      content: trimmed,
      revision,
      createdAt: new Date().toISOString(),
    });
    sending.value = true;
    error.value = '';

    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId.value)}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          clientMessageId: messageId,
          content: trimmed,
          expectedRevision: activeRevision.value || expectedRevision || undefined,
          contextRefs: contextRefs.length ? contextRefs : undefined,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const turn = await response.json() as ConversationTurnResponse;
      lastTurn.value = turn;
      if (turn.runId) {
        watchRun(turn.runId);
        if (turn.queuePosition) {
          activeRun.value = {
            ...(activeRun.value ?? { runId: turn.runId, events: [] }),
            runId: turn.runId,
            status: turn.runStatus || 'QUEUED',
            queuePosition: turn.queuePosition,
            events: activeRun.value?.events ?? [],
          };
        }
      }

      messages.value.push({
        id: optimisticId - 1,
        clientMessageId: `${messageId}:assistant`,
        role: 'ASSISTANT',
        intent: turn.intent,
        content: turn.assistantMessage,
        revision: turn.activeRevision,
        createdAt: new Date().toISOString(),
      });

      // A side question may echo the existing trace. Only an explicit revision
      // creation is allowed to advance the conversation's active trace here.
      if (turn.action === 'REVISION_CREATED' || turn.action === 'RUN_SUPERSEDED') {
        activeTraceId.value = turn.activeTraceId;
        activeRevision.value = turn.activeRevision;
      }
      await loadConversation(turn.conversationId || conversationId.value, true);
      return turn;
    } catch (caught) {
      messages.value = messages.value.filter((message) => message.id !== optimisticId);
      error.value = caught instanceof Error ? caught.message : '消息发送失败';
      if (error.value.includes('revision 已从')) {
        await loadConversation(conversationId.value, true);
      }
      return null;
    } finally {
      sending.value = false;
    }
  }

  async function controlTask(traceId: string, action: ConversationControlAction,
                             approvedPlan?: string[]): Promise<TaskControlResponse | null> {
    const requestedTraceId = traceId.trim();
    if (!requestedTraceId || controlling.value) return null;
    controlling.value = action;
    error.value = '';

    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(requestedTraceId)}/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, approvedPlan: approvedPlan?.length ? approvedPlan : undefined }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json() as TaskControlResponse;
      lastControl.value = result;
      revisions.value = revisions.value.map((revision) => (
        revision.traceId === result.traceId ? { ...revision, status: result.status } : revision
      ));
      await loadConversation(conversationId.value, true);
      return result;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : '任务控制失败';
      return null;
    } finally {
      controlling.value = '';
    }
  }

  onScopeDispose(() => {
    loadController?.abort();
    closeRunStream();
  });

  return {
    conversationId,
    activeTraceId,
    activeRevision,
    messages,
    revisions: sortedRevisions,
    loading,
    sending,
    controlling,
    error,
    lastTurn,
    lastControl,
    activeRun,
    runHistory,
    runElapsedSeconds,
    loadConversation,
    sendMessage,
    controlTask,
    cancelRun,
    createConversation,
    watchRun,
    reset,
  };
}
