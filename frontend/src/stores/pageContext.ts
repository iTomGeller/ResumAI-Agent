import { defineStore } from 'pinia';
import type { ContextRef } from '../composables/useConversation';

export type WorkspaceView =
  | 'dashboard'
  | 'positions'
  | 'candidates'
  | 'knowledge'
  | 'detail'
  | 'analytics'
  | 'ops';

export interface PageContextState {
  workspace: WorkspaceView;
  conversationId: string;
  traceId: string;
  revisionNo?: number;
  runId?: string | null;
  taskStatus: string;
  overallScore?: number | null;
  recommendation?: string | null;
  candidateId?: string;
  jobId?: string;
  knowledgeDocId?: string;
  refs: ContextRef[];
  copilotOpen: boolean;
  copilotWidth: number;
  copilotFullscreen: boolean;
}

function buildRefs(state: Omit<PageContextState, 'refs' | 'copilotOpen' | 'copilotWidth' | 'copilotFullscreen'>): ContextRef[] {
  const refs: ContextRef[] = [];
  if (state.candidateId) {
    refs.push({ type: 'candidate', id: state.candidateId, revision: state.revisionNo });
  } else if (state.conversationId) {
    refs.push({ type: 'candidate', id: state.conversationId, revision: state.revisionNo });
  }
  if (state.traceId) {
    refs.push({ type: 'application', id: state.traceId });
  }
  if (state.jobId) {
    refs.push({ type: 'job', id: state.jobId });
  }
  if (state.knowledgeDocId) {
    refs.push({ type: 'knowledge_document', id: state.knowledgeDocId });
  }
  if (state.runId) {
    refs.push({ type: 'run', id: state.runId });
  }
  return refs;
}

export const usePageContextStore = defineStore('pageContext', {
  state: (): PageContextState => ({
    workspace: 'dashboard',
    conversationId: '',
    traceId: '',
    revisionNo: undefined,
    runId: null,
    taskStatus: '',
    overallScore: null,
    recommendation: null,
    candidateId: undefined,
    jobId: undefined,
    knowledgeDocId: undefined,
    refs: [],
    copilotOpen: true,
    copilotWidth: Number(localStorage.getItem('resumai.copilotWidth') || 420),
    copilotFullscreen: false,
  }),
  getters: {
    hasConversation: (state) => !!state.conversationId || !!state.traceId,
  },
  actions: {
    setWorkspace(view: WorkspaceView) {
      this.workspace = view;
      this.syncRefs();
    },
    setCandidate(payload: {
      conversationId?: string;
      traceId?: string;
      revisionNo?: number;
      runId?: string | null;
      taskStatus?: string;
      overallScore?: number | null;
      recommendation?: string | null;
      candidateId?: string;
    } | null) {
      if (!payload) {
        this.conversationId = '';
        this.traceId = '';
        this.revisionNo = undefined;
        this.runId = null;
        this.taskStatus = '';
        this.overallScore = null;
        this.recommendation = null;
        this.candidateId = undefined;
        this.syncRefs();
        return;
      }
      this.conversationId = payload.conversationId || payload.traceId || this.conversationId;
      this.traceId = payload.traceId || this.traceId;
      this.revisionNo = payload.revisionNo;
      this.runId = payload.runId ?? null;
      this.taskStatus = payload.taskStatus || '';
      this.overallScore = payload.overallScore ?? null;
      this.recommendation = payload.recommendation ?? null;
      this.candidateId = payload.candidateId || payload.conversationId || payload.traceId;
      this.syncRefs();
    },
    setJob(jobId?: string) {
      this.jobId = jobId || undefined;
      this.syncRefs();
    },
    setRun(runId?: string | null) {
      this.runId = runId ?? null;
      this.syncRefs();
    },
    setKnowledge(docId?: string) {
      this.knowledgeDocId = docId || undefined;
      this.syncRefs();
    },
    setCopilotWidth(width: number) {
      this.copilotWidth = Math.min(720, Math.max(320, width));
      localStorage.setItem('resumai.copilotWidth', String(this.copilotWidth));
    },
    setCopilotFullscreen(full: boolean) {
      this.copilotFullscreen = full;
      document.body.classList.toggle('copilot-fullscreen-lock', full);
    },
    toggleCopilot() {
      this.copilotOpen = !this.copilotOpen;
    },
    syncRefs() {
      this.refs = buildRefs(this);
    },
  },
});
