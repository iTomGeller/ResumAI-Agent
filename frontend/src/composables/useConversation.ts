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

  async function sendMessage(content: string, expectedRevision?: number): Promise<ConversationTurnResponse | null> {
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
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const turn = await response.json() as ConversationTurnResponse;
      lastTurn.value = turn;

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
      if (turn.action === 'REVISION_CREATED') {
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

  async function controlTask(traceId: string, action: ConversationControlAction): Promise<TaskControlResponse | null> {
    const requestedTraceId = traceId.trim();
    if (!requestedTraceId || controlling.value) return null;
    controlling.value = action;
    error.value = '';

    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(requestedTraceId)}/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
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

  onScopeDispose(() => loadController?.abort());

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
    loadConversation,
    sendMessage,
    controlTask,
    reset,
  };
}
